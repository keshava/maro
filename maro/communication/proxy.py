# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

# native lib
from collections import defaultdict, namedtuple
from enum import Enum
import itertools
import json
import os
import sys
import signal
import time
from typing import List, Tuple, Dict, Union
import uuid

# third party lib
import redis

# private lib
from maro.communication import DriverType, ZmqDriver
from maro.communication import Message, SessionMessage, SessionType, TaskSessionStage, NotificationSessionStage
from maro.communication.utils import default_parameters, peers_checker, MessageCache
from maro.utils import InternalLogger, DummyLogger
from maro.utils.exception.communication_exception import RedisConnectionError, DriverTypeError, PeersMissError, \
    InformationUncompletedError


_PEER_INFO = namedtuple("PEER_INFO", ["hash_table_name", "expected_number"])
MAX_LENGTH_FOR_MESSAGE_CACHE = 1024
HOST = default_parameters.proxy.redis.host
PORT = default_parameters.proxy.redis.port
MAX_RETRIES = default_parameters.proxy.redis.max_retries
BASE_RETRY_INTERVAL = default_parameters.proxy.redis.base_retry_interval
DELAY_FOR_SLOW_JOINER = default_parameters.proxy.delay_for_slow_joiner
ENABLE_REJOIN = default_parameters.proxy.peer_rejoin.enable        # only enable at real k8s cluster or grass cluster
PEER_UPDATE_FREQUENCY = default_parameters.proxy.peer_rejoin.peers_update_frequency
MESSAGE_CACHE_FOR_EXITED_PEER = default_parameters.proxy.peer_rejoin.message_cache_for_rejoin
REJOIN_WAIT_TIME = default_parameters.proxy.peer_rejoin.max_wait_time_for_rejoin
MINIMAL_PEERS = default_parameters.proxy.peer_rejoin.minimal_peers


class Proxy:
    """The communication module is responsible for receiving and sending messages.

    There are three ways of sending messages: ``send``, ``scatter``, and ``broadcast``. Also, there are two ways to
    receive messages from other peers: ``receive`` and ``receive_by_id``.

    Args:
        group_name (str): Identifier for the group of all distributed components.
        component_type (str): Component's type in the current group.
        expected_peers (Dict): Dict of peers' information which contains peer type and expected number.
            E.g. Dict['learner': 1, 'actor': 2]
        driver_type (Enum): A type of communication driver class uses to communicate with other components.
            Defaults to ``DriverType.ZMQ``.
        driver_parameters (Dict): The arguments for communication driver class initial. Defaults to None.
        redis_address (Tuple): Hostname and port of the Redis server. Defaults to ("localhost", 6379).
        max_retries (int): Maximum number of retries before raising an exception. Defaults to 5.
        base_retry_interval (float): The time interval between attempts. Defaults to 0.1.
        log_enable (bool): Open internal logger or not. Defaults to True.
    """

    def __init__(self, group_name: str, component_type: str, expected_peers: dict,
                 driver_type: DriverType = DriverType.ZMQ, driver_parameters: dict = None,
                 redis_address=(HOST, PORT), max_retries: int = MAX_RETRIES,
                 base_retry_interval: float = BASE_RETRY_INTERVAL, enable_rejoin: bool = ENABLE_REJOIN,
                 minimal_peers: Union[float, dict] = MINIMAL_PEERS, peer_update_frequency: int = PEER_UPDATE_FREQUENCY,
                 enable_message_cache_for_rejoin: bool = MESSAGE_CACHE_FOR_EXITED_PEER,
                 max_wait_time_for_rejoin: int = REJOIN_WAIT_TIME, log_enable: bool = True):
        self._group_name = group_name
        self._component_type = component_type
        self._redis_hash_name = f"{self._group_name}:{self._component_type}"
        if os.getenv("component_name"):
            self._name = os.getenv("component_name")
        else:
            unique_id = str(uuid.uuid1()).replace("-", "")
            self._name = f"{self._component_type}_proxy_{unique_id}"
        self._driver_type = driver_type
        self._driver_parameters = driver_parameters
        self._max_retries = max_retries
        self._retry_interval = base_retry_interval
        self._log_enable = log_enable
        self._logger = InternalLogger(component_name=self._name) if self._log_enable else DummyLogger()

        try:
            self._redis_connection = redis.Redis(host=redis_address[0], port=redis_address[1])
        except Exception as e:
            raise RedisConnectionError(f"{self._name} failure to connect to redis server due to {e}")

        # Record the peer's redis information.
        self._peers_info_dict = {}
        for peer_type, number in expected_peers.items():
            self._peers_info_dict[peer_type] = _PEER_INFO(hash_table_name=f"{self._group_name}:{peer_type}",
                                                          expected_number=number)
        self._peers_socket_dict = defaultdict(dict)

        # Record connected peers' name.
        self._onboard_peers_name_dict = {}

        # Temporary store the message.
        self._message_cache = defaultdict(list)

        # Parameters for dynamic peers
        self._enable_rejoin = enable_rejoin
        if self._enable_rejoin:
            self._peer_update_frequency = peer_update_frequency
            self._max_wait_time_for_rejoin = max_wait_time_for_rejoin
            self._enable_message_cache = enable_message_cache_for_rejoin
            if self._enable_message_cache:
                self._message_cache_for_exited_peers = MessageCache(MAX_LENGTH_FOR_MESSAGE_CACHE)
            if isinstance(minimal_peers, float):
                self._minimal_peers = {peer_type: max(int(peer_info.expected_number * minimal_peers), 1)
                                       for peer_type, peer_info in self._peers_info_dict.items()}
            else:
                self._minimal_peers = {peer_type: max(int(peer_info.expected_number * minimal_peers[peer_type]), 1)
                                       for peer_type, peer_info in self._peers_info_dict.items()}

        self._join()

    def _signal_handler(self, signum, frame):
        self._redis_connection.hdel(self._redis_hash_name, self._name)
        if self._enable_rejoin:
            self._redis_connection.hdel("component-container-mapping", self._name)
        self._logger.critical(f"{self._name} received Signal: {signum} at frame: {frame}")
        sys.exit(signum)

    def _join(self):
        """Join the communication network for the experiment given by experiment_name with ID given by name.

        Specifically, it gets sockets' address for receiving (pulling) messages from its driver and uploads
        the receiving address to the Redis server. It then attempts to collect remote peers' receiving address
        by querying the Redis server. Finally, ask its driver to connect remote peers using those receiving address.
        """
        self._register_redis()
        self._get_peers_list()
        self._build_connection()
        # TODO: Handle slow joiner for PUB/SUB.
        time.sleep(DELAY_FOR_SLOW_JOINER)

        # Build component-container-mapping for dynamic component in k8s/grass cluster
        if self._enable_rejoin:
            container_name = os.getenv("CONTAINER_NAME")
            job_name = os.getenv("JOB_NAME")
            self._redis_connection.hset(f"{job_name}:component_name_to_container_name", self._name, json.dumps(container_name))

    def __del__(self):
        self._redis_connection.hdel(self._redis_hash_name, self._name)

    def _register_redis(self):
        """Self-registration on Redis and driver initialization.

        Redis store structure:
        Hash Table: name: group name + peer's type,
                    table (Dict[]): The key of table is the peer's name,
                                    the value of table is the peer's socket address.
        """
        if self._driver_type == DriverType.ZMQ:
            self._driver = ZmqDriver(**self._driver_parameters, logger=self._logger) if self._driver_parameters else \
                ZmqDriver(logger=self._logger)
        else:
            raise DriverTypeError(f"Unsupported driver type {self._driver_type}, please use DriverType class.")

        driver_address = self._driver.address
        self._redis_connection.hset(self._redis_hash_name, self._name, json.dumps(driver_address))

        # Handle interrupt signal for clearing Redis record.
        try:
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
        except Exception as e:
            self._logger.critical(f"Signal detector disable. This may cause dirty data to be left in the Redis! "
                                  f"To avoid this, please use multiprocess or make sure it can exit successfully."
                                  f"Due to {str(e)}.")

    def _get_peers_list(self):
        """To collect all peers' name in the same group (group name) from Redis."""
        if not self._peers_info_dict:
            raise PeersMissError(f"Cannot get {self._name}\'s peers.")

        for peer_type in self._peers_info_dict.keys():
            peer_hash_name, peer_number = self._peers_info_dict[peer_type]
            retry_number = 0
            expected_peers_name = []
            while retry_number < self._max_retries:
                if self._redis_connection.hlen(peer_hash_name) >= peer_number:
                    expected_peers_name = self._redis_connection.hkeys(peer_hash_name)
                    expected_peers_name = [peer.decode() for peer in expected_peers_name]
                    if len(expected_peers_name) > peer_number:
                        expected_peers_name = expected_peers_name[:peer_number]
                    self._logger.debug(f"{self._name} successfully get all {peer_type}\'s name.")
                    break
                else:
                    self._logger.debug(f"{self._name} failed to get {peer_type}\'s name. Retrying in "
                                       f"{self._retry_interval * (2 ** retry_number)} seconds.")
                    time.sleep(self._retry_interval * (2 ** retry_number))
                    retry_number += 1

            if not expected_peers_name:
                raise InformationUncompletedError(
                    f"{self._name} failure to get enough number of {peer_type} from redis.")

            self._onboard_peers_name_dict[peer_type] = expected_peers_name

        self._onboard_peers_lifetime = time.time()

    def _build_connection(self):
        """Grabbing all peers' address from Redis, and connect all peers in driver. """
        for peer_type, name_list in self._onboard_peers_name_dict.items():
            try:
                peers_socket_value = self._redis_connection.hmget(self._peers_info_dict[peer_type].hash_table_name,
                                                                  name_list)
                for idx, peer_name in enumerate(name_list):
                    self._peers_socket_dict[peer_name] = json.loads(peers_socket_value[idx])
                    self._logger.debug(f"{self._name} successfully get {peer_name}\'s socket address")
            except Exception as e:
                raise InformationUncompletedError(f"{self._name} failed to get {name_list}\'s address. Due to {str(e)}")

        self._driver.connect(self._peers_socket_dict)

    @property
    def group_name(self) -> str:
        """str: Identifier for the group of all communication components."""
        return self._group_name

    @property
    def component_name(self) -> str:
        """str: Unique identifier in the current group."""
        return self._name

    @property
    def peers(self) -> Dict:
        """Dict: The ``Dict`` of all connected peers' names, stored by peer type."""
        return self._onboard_peers_name_dict

    def receive(self, is_continuous: bool = True):
        """Receive messages from communication driver.

        Args:
            is_continuous (bool): Continuously receive message or not. Defaults to True.
        """
        return self._driver.receive(is_continuous)

    def receive_by_id(self, session_id_list: list) -> List[Message]:
        """Receive target messages from communication driver.

        Args:
            session_id_list List[str]: List of ``session_id``.
                E.g. ['0_learner0_actor0', '1_learner1_actor1', ...].

        Returns:
            List[Message]: List of received messages.
        """
        pending_message_list = session_id_list[:]
        received_message = []

        # Check message cache for saved messages.
        for msg_key in session_id_list:
            if msg_key in list(self._message_cache.keys()):
                for msg in self._message_cache[msg_key]:
                    pending_message_list.remove(msg_key)
                    received_message.append(msg)
                del self._message_cache[msg_key]

        if not pending_message_list:
            return received_message

        # Wait for incoming messages.
        for msg in self._driver.receive():
            msg_key = msg.session_id

            if msg_key in pending_message_list:
                pending_message_list.remove(msg_key)
                received_message.append(msg)
            else:
                self._message_cache[msg_key].append(msg)

            if not pending_message_list:
                break

        return received_message

    def _scatter(self, tag: Union[str, Enum], session_type: SessionType, destination_payload_list: list,
                 session_id: str = None) -> List[str]:
        """Scatters a list of data to peers, and return list of session id."""
        session_id_list = []

        for destination, payload in destination_payload_list:
            message = SessionMessage(tag=tag,
                                     source=self._name,
                                     destination=destination,
                                     session_id=session_id,
                                     payload=payload,
                                     session_type=session_type)
            session_id_list.append(self.isend(message))

        # Flatten.
        session_id_list = list(itertools.chain.from_iterable(session_id_list))
        return session_id_list

    def scatter(self, tag: Union[str, Enum], session_type: SessionType, destination_payload_list: list,
                session_id: str = None) -> List[Message]:
        """Scatters a list of data to peers, and return replied messages.

        Args:
            tag (str|Enum): Message's tag.
            session_type (Enum): Message's session type.
            destination_payload_list ([Tuple(str, object)]): The destination-payload list.
                The first item of the tuple in list is the message destination,
                and the second item of the tuple in list is the message payload.
            session_id (str): Message's session id. Defaults to None.

        Returns:
            List[Message]: List of replied message.
        """
        return self.receive_by_id(self._scatter(tag, session_type, destination_payload_list, session_id))

    def iscatter(self, tag: Union[str, Enum], session_type: SessionType, destination_payload_list: list,
                 session_id: str = None) -> List[str]:
        """Scatters a list of data to peers, and return list of message id.

        Args:
            tag (str|Enum): Message's tag.
            session_type (Enum): Message's session type.
            destination_payload_list ([Tuple(str, object)]): The destination-payload list.
                The first item of the tuple in list is the message's destination,
                and the second item of the tuple in list is the message's payload.
            session_id (str): Message's session id. Defaults to None.

        Returns:
            List[str]: List of message's session id.
        """
        return self._scatter(tag, session_type, destination_payload_list, session_id)

    @peers_checker
    def _broadcast(self, tag: Union[str, Enum], session_type: SessionType,
                   session_id: str = None, payload=None) -> List[str]:
        """Broadcast message to all peers, and return list of session id."""
        message = SessionMessage(tag=tag,
                                 source=self._name,
                                 destination="*",
                                 payload=payload,
                                 session_id=session_id,
                                 session_type=session_type)

        self._driver.broadcast(message)

        return [message.session_id] * len(list(itertools.chain.from_iterable(self._onboard_peers_name_dict.values())))

    def broadcast(self, tag: Union[str, Enum], session_type: SessionType,
                  session_id: str = None, payload=None) -> List[Message]:
        """Broadcast message to all peers, and return all replied messages.

        Args:
            tag (str|Enum): Message's tag.
            session_type (Enum): Message's session type.
            session_id (str): Message's session id. Defaults to None.
            payload (object): The true data. Defaults to None.

        Returns:
            List[Message]: List of replied messages.
        """
        return self.receive_by_id(self._broadcast(tag, session_type, session_id, payload))

    def ibroadcast(self, tag: Union[str, Enum], session_type: SessionType,
                   session_id: str = None, payload=None) -> List[str]:
        """Broadcast message to all subscribers, and return list of message's session id.

        Args:
            tag (str|Enum): Message's tag.
            session_type (Enum): Message's session type.
            session_id (str): Message's session id. Defaults to None.
            payload (object): The true data. Defaults to None.

        Returns:
            List[str]: List of message's session id which related to the replied message.
        """
        return self._broadcast(tag, session_type, session_id, payload)

    @peers_checker
    def isend(self, message: Message) -> List[str]:
        """Send a message to a remote peer.

        Args:
            message: Message to be sent.

        Returns:
            List[str]: List of message's session id.
        """
        sending_status = self._driver.send(message)

        return [message.session_id] if not sending_status else sending_status

    @peers_checker
    def send(self, message: Message) -> List[Message]:
        """Send a message to a remote peer.

        Args:
            message: Message to be sent.

        Returns:
            List[Message]: List of replied message.
        """
        sending_status = self._driver.send(message)

        return self.receive_by_id([message.session_id]) if not sending_status else sending_status

    def reply(self, received_message: SessionMessage, tag: Union[str, Enum] = None, payload=None,
              ack_reply: bool = False) -> List[str]:
        """Reply a received message.

        Args:
            received_message (Message): The message need to reply.
            tag (str|Enum): New message tag, if None, keeps the original message's tag. Defaults to None.
            payload (object): New message payload, if None, keeps the original message's payload. Defaults to None.
            ack_reply (bool): If True, it is acknowledge reply. Defaults to False.

        Returns:
            List[str]: Message belonged session id.
        """
        if received_message.session_type == SessionType.TASK:
            session_stage = TaskSessionStage.RECEIVE if ack_reply else TaskSessionStage.COMPLETE
        else:
            session_stage = NotificationSessionStage.RECEIVE

        replied_message = SessionMessage(tag=tag if tag else received_message.tag,
                                         source=self._name,
                                         destination=received_message.source,
                                         session_id=received_message.session_id,
                                         payload=payload,
                                         session_stage=session_stage)
        return self.isend(replied_message)

    def forward(self, received_message: SessionMessage, destination: str, tag: Union[str, Enum] = None,
                payload=None) -> List[str]:
        """Forward a received message.

        Args:
            received_message (Message): The message need to forward.
            destination (str): The receiver of message.
            tag (str|Enum): New message tag, if None, keeps the original message's tag. Defaults to None.
            payload (object): Message payload, if None, keeps the original message's payload. Defaults to None.

        Returns:
            List[str]: Message belonged session id.
        """
        forward_message = SessionMessage(tag=tag if tag else received_message.tag,
                                         source=self._name,
                                         destination=destination,
                                         session_id=received_message.session_id,
                                         payload=payload if payload else received_message.payload,
                                         session_stage=received_message.session_stage)
        return self.isend(forward_message)

    def _check_peers_update(self):
        for peer_type, on_board_peer_name_list in self._onboard_peers_name_dict.items():
            on_redis_peers_dict = self._redis_connection.hgetall(self._peers_info_dict[peer_type].hash_table_name)
            # decode
            on_redis_peers_dict = {key.decode(): json.loads(value) for key, value in on_redis_peers_dict.items()}
            on_board_peers_dict = {onboard_peer_name: self._peers_socket_dict[onboard_peer_name]
                                   for onboard_peer_name in on_board_peer_name_list}

            if on_board_peers_dict != on_redis_peers_dict:
                for peer_name, socket_info in on_redis_peers_dict.items():
                    # New peer joined.
                    if peer_name not in on_board_peers_dict.keys():
                        self._logger.debug(f"PEER_REJOIN: New peer {peer_name} join.")
                        self._driver.connect({peer_name: socket_info})
                        self._peers_socket_dict[peer_name] = socket_info
                    else:
                        # Old peer restarted.
                        if socket_info != on_board_peers_dict[peer_name]:
                            self._logger.debug(f"PEER_REJOIN: Peer {peer_name} rejoin.")
                            self._driver.disconnect({peer_name: on_board_peers_dict[peer_name]})
                            self._driver.connect({peer_name: socket_info})
                            self._peers_socket_dict[peer_name] = socket_info

                # Onboard peer exited.
                exited_peers = [peer_name for peer_name in on_board_peers_dict.keys()
                                if peer_name not in on_redis_peers_dict.keys()]
                for exited_peer in exited_peers:
                    self._logger.debug(f"PEER_REJOIN: Peer {exited_peer} exited.")
                    self._driver.disconnect({exited_peer: on_board_peers_dict[exited_peer]})
                    del self._peers_socket_dict[exited_peer]

                # update peer dict
                self._onboard_peers_name_dict[peer_type] = list(on_redis_peers_dict.keys())

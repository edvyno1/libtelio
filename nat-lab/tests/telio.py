import asyncio
import json
import os
import re
import shlex
from collections import Counter
from config import DERP_PRIMARY, DERP_SERVERS
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from dataclasses_json import dataclass_json, DataClassJsonMixin
from enum import Enum
from mesh_api import Node
from telio_features import TelioFeatures
from typing import List, Dict, Any, Set, Optional, AsyncIterator
from utils import testing, asyncio_util
from utils.connection import Connection, TargetOS
from utils.connection_util import get_libtelio_binary_path
from utils.output_notifier import OutputNotifier
from utils.process import Process
from utils.router import Router, new_router


# Equivalent of `libtelio/telio-wg/src/uapi.rs`
class State(Enum):
    Disconnected = "disconnected"
    Connecting = "connecting"
    Connected = "connected"


# Equivalent of `libtelio/crates/telio-model/src/api_config.rs:PathType`
class PathType(Enum):
    Relay = "relay"
    Direct = "direct"


@dataclass_json
@dataclass
class DerpServer(DataClassJsonMixin):
    region_code: str
    name: str
    hostname: str
    ipv4: str
    relay_port: int
    stun_port: int
    stun_plaintext_port: int
    public_key: str
    weight: int
    use_plain_text: bool
    conn_state: State
    # Only for compatibility with telio v3.6
    used: bool = False

    def __hash__(self):
        return hash(
            (
                self.region_code,
                self.name,
                self.hostname,
                self.ipv4,
                self.relay_port,
                self.stun_port,
                self.stun_plaintext_port,
                self.public_key,
                self.weight,
                self.use_plain_text,
                self.conn_state,
            )
        )


# Equivalent of `libtelio/crates/telio-model/src/mesh.rs:Node`
@dataclass_json
@dataclass
class PeerInfo(DataClassJsonMixin):
    identifier: str = ""
    public_key: str = ""
    state: State = State.Disconnected
    is_exit: bool = False
    is_vpn: bool = False
    ip_addresses: List[str] = field(default_factory=lambda: [])
    allowed_ips: List[str] = field(default_factory=lambda: [])
    endpoint: Optional[str] = None
    hostname: Optional[str] = None
    allow_incoming_connections: bool = False
    allow_peer_send_files: bool = False
    path: PathType = PathType.Relay

    def __hash__(self):
        return hash(
            (
                self.identifier,
                self.public_key,
                self.state,
                self.is_exit,
                self.is_vpn,
                tuple(self.ip_addresses),
                tuple(self.allowed_ips),
                self.endpoint,
                self.hostname,
                self.allow_incoming_connections,
                self.allow_peer_send_files,
                self.path,
            )
        )

    def __eq__(self, other):
        if not isinstance(other, PeerInfo):
            return False
        return (
            self.identifier == other.identifier
            and self.public_key == other.public_key
            and self.state == other.state
            and self.is_exit == other.is_exit
            and self.is_vpn == other.is_vpn
            and self.ip_addresses == other.ip_addresses
            and self.allowed_ips == other.allowed_ips
            and (
                self.endpoint is None
                or other.endpoint is None
                or self.endpoint == other.endpoint
            )
            and (
                self.hostname is None
                or other.hostname is None
                or self.hostname == other.hostname
            )
            and self.allow_incoming_connections == other.allow_incoming_connections
            and self.allow_peer_send_files == other.allow_peer_send_files
            and self.path == other.path
        )


# Equivalent of `libtelio/telio-wg/src/adapter/mod.rs`
class AdapterType(Enum):
    Default = ""
    BoringTun = "boringtun"
    LinuxNativeWg = "linux-native"
    WireguardGo = "wireguard-go"
    WindowsNativeWg = "wireguard-nt"


class Runtime:
    _output_notifier: OutputNotifier
    _peer_state_events: List[PeerInfo]
    _derp_state_events: List[DerpServer]
    _started_tasks: List[str]
    _stopped_tasks: List[str]
    allowed_pub_keys: Set[str]

    def __init__(self) -> None:
        self._output_notifier = OutputNotifier()
        self._peer_state_events = []
        self._derp_state_events = []
        self._started_tasks = []
        self._stopped_tasks = []
        self.allowed_pub_keys = set()

    def handle_output_line(self, line) -> bool:
        return (
            self._handle_node_event(line)
            or self._output_notifier.handle_output(line)
            or self._handle_derp_event(line)
            or self._handle_task_information(line)
        )

    def _handle_task_information(self, line) -> bool:
        if line.startswith("task started - "):
            tokens = line.split("task started - ")
            self._started_tasks.append(tokens[1].strip())
            return True

        if line.startswith("task stopped - "):
            tokens = line.split("task stopped - ")
            self._stopped_tasks.append(tokens[1].strip())
            return True

        return False

    def get_output_notifier(self) -> OutputNotifier:
        return self._output_notifier

    async def notify_peer_state(
        self,
        public_key: str,
        states: List[State],
        paths: List[PathType],
    ) -> None:
        while True:
            peer = self.get_peer_info(public_key)
            if peer and peer.path in paths and peer.state in states:
                return
            await asyncio.sleep(0.1)

    async def notify_peer_event(
        self,
        public_key: str,
        states: List[State],
        paths: List[PathType],
    ) -> None:
        def _get_events() -> List[PeerInfo]:
            return [
                peer
                for peer in self._peer_state_events
                if peer
                and peer.public_key == public_key
                and peer.path in paths
                and peer.state in states
            ]

        old_events = _get_events()

        while True:
            new_events = _get_events()[len(old_events) :]
            if new_events:
                return
            await asyncio.sleep(0.1)

    def get_peer_info(self, public_key: str) -> Optional[PeerInfo]:
        events = [
            peer_event
            for peer_event in self._peer_state_events
            if peer_event.public_key == public_key
        ]
        if events:
            return events[-1]
        return None

    async def notify_derp_state(
        self,
        server_ip: str,
        states: List[State],
    ) -> None:
        while True:
            derp = self.get_derp_info(server_ip)
            if derp and derp.ipv4 == server_ip and derp.conn_state in states:
                return
            await asyncio.sleep(0.1)

    async def notify_derp_event(
        self,
        server_ip: str,
        states: List[State],
    ) -> None:
        def _get_events() -> List[DerpServer]:
            return [
                event
                for event in self._derp_state_events
                if event.ipv4 == server_ip and event.conn_state in states
            ]

        old_events = _get_events()

        while True:
            new_events = _get_events()[len(old_events) :]
            if new_events:
                return
            await asyncio.sleep(0.1)

    def get_derp_info(self, server_ip: str) -> Optional[DerpServer]:
        events = [event for event in self._derp_state_events if event.ipv4 == server_ip]
        if events:
            return events[-1]
        return None

    def set_peer(self, peer: PeerInfo) -> None:
        assert peer.public_key in self.allowed_pub_keys
        self._peer_state_events.append(peer)

    def _handle_node_event(self, line) -> bool:
        if not line.startswith("event node: "):
            return False
        tokens = line.split("event node: ")
        json_string = tokens[1].strip()
        result = re.search("{(.*)}", json_string)
        if result:
            node_state = PeerInfo.from_json(
                "{" + result.group(1).replace("\\", "") + "}"
            )
            assert isinstance(node_state, PeerInfo)
            self.set_peer(node_state)
            return True
        return False

    def set_derp(self, derp: DerpServer) -> None:
        self._derp_state_events.append(derp)

    def _handle_derp_event(self, line) -> bool:
        if not line.startswith("event relay: "):
            return False
        tokens = line.split("event relay: ")
        json_string = tokens[1].strip()
        result = re.search("{(.*)}", json_string)
        if result:
            derp_server_json = DerpServer.from_json(
                # Added "used" variable for compatibility with telio 3.6
                "{"
                + result.group(1).replace("\\", "")
                + "}"
            )
            assert isinstance(derp_server_json, DerpServer)
            self.set_derp(derp_server_json)
            return True
        return False

    def get_started_tasks(self) -> List[str]:
        return self._started_tasks

    def get_stopped_tasks(self) -> List[str]:
        return self._stopped_tasks


class Events:
    _runtime: Runtime

    def __init__(
        self,
        runtime: Runtime,
    ) -> None:
        self._runtime = runtime

    async def message_done(self, message_idx: int) -> None:
        event = asyncio.Event()
        self._runtime.get_output_notifier().notify_output(
            f"MESSAGE_DONE={message_idx}", event
        )
        await event.wait()

    async def wait_for_state_peer(
        self,
        public_key: str,
        state: List[State],
        path: List[PathType],
    ) -> None:
        await self._runtime.notify_peer_state(public_key, state, path)

    async def wait_for_event_peer(
        self,
        public_key: str,
        states: List[State],
        paths: List[PathType],
    ) -> None:
        await self._runtime.notify_peer_event(public_key, states, paths)

    async def wait_for_state_derp(
        self,
        server_ip: str,
        states: List[State],
    ) -> None:
        await self._runtime.notify_derp_state(server_ip, states)

    async def wait_for_event_derp(
        self,
        server_ip: str,
        states: List[State],
    ) -> None:
        await self._runtime.notify_derp_event(server_ip, states)


class Client:
    def __init__(
        self,
        connection: Connection,
        node: Node,
        adapter_type: AdapterType = AdapterType.Default,
        telio_features: TelioFeatures = TelioFeatures(),
    ) -> None:
        self._router: Optional[Router] = None
        self._events: Optional[Events] = None
        self._runtime: Optional[Runtime] = None
        self._process: Optional[Process] = None
        self._interface_configured = False
        self._message_idx = 0
        self._node = node
        self._connection = connection
        self._adapter_type = adapter_type
        self._telio_features = telio_features
        self._quit = False

    @asynccontextmanager
    async def run(self, telio_v3=False) -> AsyncIterator["Client"]:
        async def on_stdout(stdout: str) -> None:
            supress_print_list = [
                "MESSAGE_DONE=",
                "- no login.",
                "- telio running.",
                "- telio nodes",
                "task stopped - ",
                "task started - ",
            ]
            for line in stdout.splitlines():
                if not any(string in line for string in supress_print_list):
                    print(f"[{self._node.name}]: {line}")
                if self._runtime:
                    self._runtime.handle_output_line(line)

        tcli_path = get_libtelio_binary_path("tcli", self._connection)

        self._runtime = Runtime()
        self._events = Events(self._runtime)
        self._router = new_router(self._connection, self._node.ip_stack)
        if telio_v3:
            self._process = self._connection.create_process(
                [
                    "/opt/bin/tcli-3.6",
                    "--less-spam",
                    '-f { "paths": { "priority": ["relay", "udp-hole-punch"]} }',
                ]
            )
        else:
            self._process = self._connection.create_process(
                [
                    tcli_path,
                    "--less-spam",
                    f"-f {self._telio_features.to_json()}",
                ]
            )
        async with self._process.run(stdout_callback=on_stdout):
            try:
                await self._process.wait_stdin_ready()
                await self._write_command(
                    [
                        "dev",
                        "start",
                        self._adapter_type.value,
                        self._router.get_interface_name(),
                        self._node.private_key,
                    ],
                )
                async with asyncio_util.run_async_context(self._event_request_loop()):
                    yield self
            finally:
                if self._process.is_executing():
                    await testing.wait_long(self.stop_device())
                    self._quit = True
                if self._router:
                    await self._router.delete_vpn_route()
                    await self._router.delete_exit_node_route()
                    await self._router.delete_interface()
                await self.save_logs(self._connection)

    @asynccontextmanager
    async def run_meshnet(
        self, meshmap: Dict[str, Any], telio_v3=False
    ) -> AsyncIterator["Client"]:
        async with self.run(telio_v3):
            await self.set_meshmap(meshmap)
            yield self

    async def quit(self):
        self._quit = True
        await self._write_command(["quit"])

    async def simple_start(self):
        await self._write_command(
            [
                "dev",
                "start",
                self._adapter_type.value,
                self.get_router().get_interface_name(),
                self._node.private_key,
            ],
        )

    async def wait_for_state_peer(
        self, public_key, states: List[State], paths: Optional[List[PathType]] = None
    ) -> None:
        await self.get_events().wait_for_state_peer(
            public_key, states, paths if paths else [PathType.Relay]
        )

    async def wait_for_event_peer(
        self,
        public_key: str,
        states: List[State],
        paths: Optional[List[PathType]] = None,
    ) -> None:
        await self.get_events().wait_for_event_peer(
            public_key, states, paths if paths else [PathType.Relay]
        )

    async def wait_for_state_derp(self, derp_ip, states: List[State]) -> None:
        await self.get_events().wait_for_state_derp(derp_ip, states)

    async def wait_for_state_on_any_derp(self, states: List[State]) -> None:
        async with asyncio_util.run_async_contexts(
            [
                self.get_events().wait_for_state_derp(str(derp["ipv4"]), states)
                for derp in DERP_SERVERS
            ]
        ) as futures:
            try:
                while not any(fut.done() for fut in futures):
                    await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                pass

    async def wait_for_every_derp_disconnection(self) -> None:
        async with asyncio_util.run_async_contexts(
            [
                self.get_events().wait_for_state_derp(
                    str(derp["ipv4"]), [State.Disconnected, State.Connecting]
                )
                for derp in DERP_SERVERS
            ]
        ) as futures:
            try:
                while not all(fut.done() for fut in futures):
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass

    async def wait_for_event_derp(self, derp_ip, states: List[State]) -> None:
        await self.get_events().wait_for_event_derp(derp_ip, states)

    async def wait_for_event_on_any_derp(self, states: List[State]) -> None:
        async with asyncio_util.run_async_contexts(
            [
                self.get_events().wait_for_event_derp(str(derp["ipv4"]), states)
                for derp in DERP_SERVERS
            ]
        ) as futures:
            try:
                while not any(fut.done() for fut in futures):
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                pass

    async def set_meshmap(self, meshmap: Dict[str, Any]) -> None:
        made_changes = await self._configure_interface()

        # Linux native WG takes ~1.5s to setup listen port for WG interface. Since
        # listen port is required for mesh connection, there is no other way to
        # bypass this waiting period. Another option would be to retry `mesh config`
        # multiple times until the command succeeds.
        if made_changes and self._adapter_type == AdapterType.LinuxNativeWg:
            await asyncio.sleep(2)

        if "peers" in meshmap:
            peers = meshmap["peers"]
            for peer in peers:
                self.get_runtime().allowed_pub_keys.add(peer["public_key"])

        await self._write_command(["mesh", "config", json.dumps(meshmap)])

    async def set_mesh_off(self):
        await self._write_command(["mesh", "off"])

    async def receive_ping(self):
        await self._write_command(["mesh", "ping"])

    async def send_stun(self):
        await self._write_command(["mesh", "stun"])

    async def igd(self):
        await self._write_command(["mesh", "igd"])

    async def connect_to_vpn(self, ip, port, public_key) -> None:
        await self._configure_interface()
        await self.get_router().create_vpn_route()
        self.get_runtime().allowed_pub_keys.add(public_key)
        await self._write_command(["dev", "con", public_key, f"{ip}:{port}"])

    async def disconnect_from_vpn(
        self, public_key, paths: Optional[List[PathType]] = None
    ) -> None:
        await self._write_command(["vpn", "off"])
        await self.wait_for_state_peer(
            public_key, [State.Disconnected], paths if paths else [PathType.Relay]
        )
        await self.get_router().delete_vpn_route()

    async def disconnect_from_exit_nodes(self) -> None:
        await self._write_command(["vpn", "off"])
        await self.get_router().delete_vpn_route()

    async def enable_magic_dns(self, forward_servers: List[str]) -> None:
        await self._write_command(["dns", "on"] + forward_servers)

    async def disable_magic_dns(self) -> None:
        await self._write_command(["dns", "off"])

    async def notify_network_change(self) -> None:
        await self._write_command(["dev", "notify-net-change"])

    async def _configure_interface(self) -> bool:
        if not self._interface_configured:
            await self.get_router().setup_interface(self._node.ip_addresses)

            await self.get_router().create_meshnet_route()
            self._interface_configured = True
            return True

        return False

    async def connect_to_exit_node(self, public_key: str) -> None:
        await self._configure_interface()
        await self.get_router().create_vpn_route()
        await self._write_command(["dev", "con", public_key])

    def get_router(self) -> Router:
        assert self._router
        return self._router

    def get_runtime(self) -> Runtime:
        assert self._runtime
        return self._runtime

    def get_process(self) -> Process:
        assert self._process
        return self._process

    def get_events(self) -> Events:
        assert self._events
        return self._events

    def get_stdout(self) -> str:
        assert self._process
        return self._process.get_stdout()

    async def stop_device(self) -> None:
        await self._write_command(["dev", "stop"])
        self._interface_configured = False
        assert Counter(self.get_runtime().get_started_tasks()) == Counter(
            self.get_runtime().get_stopped_tasks()
        ), "started tasks and stopped tasks differ!"

    def get_node_state(self, public_key: str) -> Optional[PeerInfo]:
        return self.get_runtime().get_peer_info(public_key)

    def get_derp_state(self, server_ip: str) -> Optional[DerpServer]:
        return self.get_runtime().get_derp_info(server_ip)

    async def _event_request_loop(self) -> None:
        while True:
            try:
                await self._write_command(["events"])
                await asyncio.sleep(1)
            except:
                if self._quit:
                    return
                raise

    async def create_fake_derprelay_to_derp01(self, sk: str, allowed_pk: str) -> None:
        derp01_server = (
            '{"host":"'
            + str(DERP_PRIMARY["hostname"])
            + '","ipv4":"'
            + str(DERP_PRIMARY["ipv4"])
            + '","port":'
            + str(DERP_PRIMARY["relay_port"])
            + ',"pk":"'
            + str(DERP_PRIMARY["public_key"])
            + '"}'
        )

        await self._write_command(["derp", "on", sk, derp01_server, allowed_pk])

    async def disconnect_fake_derprelay(self) -> None:
        await self._write_command(["derp", "off"])

    async def recv_message_from_fake_derp_relay(self) -> None:
        await self._write_command(["derp", "recv"])

    async def fake_derp_events(self) -> None:
        await self._write_command(["derp", "events"])

    async def send_message_from_fake_derp_relay(self, pk: str, data: List[str]) -> None:
        await self._write_command(["derp", "send", pk] + data)

    async def trigger_event_collection(self) -> None:
        await self._write_command(["dev", "analytics"])

    async def _write_command(self, command: List[str]) -> None:
        idx = self._message_idx
        cmd = (
            f"MESSAGE_ID={str(idx)} "
            + " ".join([shlex.quote(arg) for arg in command])
            + "\n"
        )
        await self.get_process().write_stdin(cmd)
        self._message_idx += 1
        await self.get_events().message_done(idx)

    def get_endpoint_address(self, public_key: str) -> str:
        node = self.get_node_state(public_key)
        if node is None:
            raise Exception(f"Node {public_key} doesn't exist")
        if node.endpoint is None:
            raise Exception(f"Node {public_key} endpoint doesn't exist")
        return node.endpoint.split(":")[0]

    def wait_for_output(self, what: str) -> asyncio.Event:
        event = asyncio.Event()
        self.get_runtime().get_output_notifier().notify_output(what, event)
        return event

    @staticmethod
    async def save_logs(connection: Connection) -> None:
        if os.environ.get("NATLAB_SAVE_LOGS") is None:
            return

        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)

        process = (
            connection.create_process(["type", "tcli.log"])
            if connection.target_os == TargetOS.Windows
            else connection.create_process(["cat", "./tcli.log"])
        )
        await process.execute()
        log_content = process.get_stdout()

        if connection.target_os == TargetOS.Linux:
            process = connection.create_process(["cat", "/etc/hostname"])
            await process.execute()
            container_id = process.get_stdout().strip()
        else:
            container_id = str(connection.target_os)

        test_name = os.environ.get("PYTEST_CURRENT_TEST")
        if test_name is not None:
            test_name = "".join(
                [x if x.isalnum() else "_" for x in test_name.split(" ")[0]]
            )
        with open(
            os.path.join(log_dir, str(test_name) + "_" + container_id + ".log"),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(log_content)

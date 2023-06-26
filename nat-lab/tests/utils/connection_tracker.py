import re
import asyncio
import time
from typing import Coroutine, Optional, List, Dict
from dataclasses import dataclass
from utils.ping import Ping
from utils.asyncio_util import run_async, cancel_future
from utils.connection import Connection
from utils.process import Process


@dataclass
class FiveTuple:
    protocol: Optional[str] = None
    src_ip: Optional[str] = None
    dst_ip: Optional[str] = None
    src_port: Optional[int] = None
    dst_port: Optional[int] = None

    def partial_eq(self, comparison: "FiveTuple") -> bool:
        return (
            (self.protocol is None or self.protocol == comparison.protocol)
            and (self.src_ip is None or self.src_ip == comparison.src_ip)
            and (self.dst_ip is None or self.dst_ip == comparison.dst_ip)
            and (self.src_port is None or self.src_port == comparison.src_port)
            and (self.dst_port is None or self.dst_port == comparison.dst_port)
        )


@dataclass
class ConnectionLimits:
    min: Optional[int] = None
    max: Optional[int] = None


@dataclass
class ConnectionTrackerConfig:
    key: str
    limits: ConnectionLimits
    target: FiveTuple


def parse_input(input_string) -> FiveTuple:
    five_tuple = FiveTuple()

    match = re.search(r"\[NEW\] (\w+)", input_string)
    if match:
        five_tuple.protocol = match.group(1)

    match = re.search(r"src=([^\s]+)", input_string)
    if match:
        five_tuple.src_ip = match.group(1)

    match = re.search(r"dst=([^\s]+)", input_string)
    if match:
        five_tuple.dst_ip = match.group(1)

    match = re.search(r"sport=(\d+)", input_string)
    if match:
        five_tuple.src_port = int(match.group(1))

    match = re.search(r"dport=(\d+)", input_string)
    if match:
        five_tuple.dst_port = int(match.group(1))

    return five_tuple


class ConnectionTracker:
    def __init__(
        self,
        connection: Connection,
        config: Optional[List[ConnectionTrackerConfig]] = None,
    ):
        self._process: Process = connection.create_process(
            ["conntrack", "-E", "-e", "NEW"]
        )
        self._connection: Connection = connection
        self._stop: Optional[Coroutine] = None
        self._config: Optional[List[ConnectionTrackerConfig]] = config
        self._events: List[FiveTuple] = []

        self._initialized: bool = False
        self._init_connection: FiveTuple = FiveTuple(
            protocol="icmp", dst_ip="127.0.0.1"
        )

    def execute(self) -> "ConnectionTracker":
        if not self._config:
            return self

        async def _on_stdout(stdout: str) -> None:
            for line in stdout.splitlines():
                connection = parse_input(line)
                if connection is FiveTuple():
                    continue

                if not self._initialized:
                    if self._init_connection.partial_eq(connection):
                        self._initialized = True
                        continue

                self._events.append(connection)

        command_coroutine = run_async(self._process.execute(stdout_callback=_on_stdout))

        async def stop() -> None:
            if not self._config:
                return

            await cancel_future(command_coroutine)
            await self._connection.create_process(["killall", "conntrack"]).execute()

        self._stop = stop()

        return self

    def get_out_of_limits(self) -> Optional[Dict[str, int]]:
        if not self._config:
            return None

        out_of_limit_connections: Dict[str, int] = {}

        for cfg in self._config:
            count = len(
                [event for event in self._events if cfg.target.partial_eq(event)]
            )
            if cfg.limits.max is not None:
                if count > cfg.limits.max:
                    out_of_limit_connections[cfg.key] = count
                    continue
            if cfg.limits.min is not None:
                if count < cfg.limits.min:
                    out_of_limit_connections[cfg.key] = count
                    continue

        return out_of_limit_connections if bool(out_of_limit_connections) else None

    async def wait_for_event(self, key: str) -> None:
        if not self._config:
            return

        cfg = next((cfg for cfg in self._config if cfg.key == key), None)
        if cfg is None:
            return

        while True:
            if [event for event in self._events if cfg.target.partial_eq(event)]:
                return
            await asyncio.sleep(0.1)

    async def stop(self) -> None:
        if self._stop:
            await self._stop

    async def __aenter__(self) -> "ConnectionTracker":
        if not self._config:
            return self
        connection_tracker = self.execute()
        await self._process.wait_stdin_ready()

        # initialization is just waiting for first conntrack event,
        # since it has no other indication if it is truly running.
        # Or wait for 1 second and pray it was initialized
        async with Ping(self._connection, "127.0.0.1"):
            start_time = time.time()
            while not self._initialized:
                if time.time() - start_time >= 1:
                    self._initialized = True
                    break
                await asyncio.sleep(0.1)

        return connection_tracker

    async def __aexit__(self, exc_type, exc, tb):
        await self.stop()
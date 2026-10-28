"""本地 AirSim RPC 通信。"""

import socket

import msgpack


class AirSimClient:
    def __init__(self, timeout=3):
        self.timeout = timeout
        self.socket = None
        self.serial = 0

    def close(self):
        if self.socket:
            self.socket.close()
        self.socket = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def call(self, method, *args, timeout=None):
        try:
            if self.socket is None:
                self.socket = socket.create_connection(("127.0.0.1", 41451), self.timeout)
                self.socket.settimeout(self.timeout)
                self.unpacker = msgpack.Unpacker(raw=False, unicode_errors="surrogateescape", max_buffer_size=64 * 1024 * 1024)
            self.socket.settimeout(self.timeout if timeout is None else timeout)
            self.serial += 1
            self.socket.sendall(msgpack.packb([0, self.serial, method, list(args)], use_bin_type=True))
            while True:
                for response in self.unpacker:
                    if response[0] == 1 and response[1] == self.serial:
                        if response[2] is not None:
                            raise RuntimeError(str(response[2]))
                        return response[3]
                block = self.socket.recv(65536)
                if not block:
                    raise ConnectionError("AirSim RPC connection closed")
                self.unpacker.feed(block)
        except Exception:
            self.close()
            raise

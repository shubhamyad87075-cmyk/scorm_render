class ProxyManager:
    def __init__(self, config: dict):
        self.config = config["proxy"]
        self.servers = self.config["servers"]

    def get_proxy(self, country: str) -> dict:
        server = self.servers.get(country, self.servers["us"])
        return {
            "server": f"socks5://{server}:{self.config['port']}",
            "username": self.config["username"],
            "password": self.config["password"]
        }

import dataclasses


@dataclasses.dataclass
class LoginRequest:
    email: str
    password: str

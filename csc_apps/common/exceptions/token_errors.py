class TokenErrors(Exception):
    def __init__(self, errors: list):
        self.errors = errors

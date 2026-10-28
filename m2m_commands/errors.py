class ApiError(RuntimeError):
    """Помилка API платформи. `status` — HTTP-код (0, якщо відповіді не було)."""

    def __init__(self, message: str, status: int = 0, uncertain: bool = False):
        super().__init__(message)
        self.status = status
        # True — запит міг дійти до сервера й виконатись (обрив/таймаут після відправки, 5xx).
        self.uncertain = uncertain

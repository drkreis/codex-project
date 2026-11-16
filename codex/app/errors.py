class UserError(ValueError):
    """An actionable validation error that can be displayed without a traceback."""

    def __init__(self, message: str, field: str = "", status_code: int = 422):
        super().__init__(message)
        self.message = message
        self.field = field
        self.status_code = status_code

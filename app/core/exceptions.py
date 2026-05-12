from fastapi import HTTPException, status


class IACMException(HTTPException):
    def __init__(self, status_code: int, detail: str, error_code: str = None):
        super().__init__(status_code=status_code, detail={"message": detail, "error_code": error_code})


class NotFoundError(IACMException):
    def __init__(self, resource: str, resource_id: str = None):
        detail = f"{resource} not found" if not resource_id else f"{resource} with id '{resource_id}' not found"
        super().__init__(status.HTTP_404_NOT_FOUND, detail, "NOT_FOUND")


class UnauthorizedError(IACMException):
    def __init__(self, detail: str = "Not authenticated"):
        super().__init__(status.HTTP_401_UNAUTHORIZED, detail, "UNAUTHORIZED")


class ForbiddenError(IACMException):
    def __init__(self, detail: str = "Insufficient permissions"):
        super().__init__(status.HTTP_403_FORBIDDEN, detail, "FORBIDDEN")


class ConflictError(IACMException):
    def __init__(self, detail: str):
        super().__init__(status.HTTP_409_CONFLICT, detail, "CONFLICT")


class ValidationError(IACMException):
    def __init__(self, detail: str):
        super().__init__(status.HTTP_422_UNPROCESSABLE_ENTITY, detail, "VALIDATION_ERROR")


class FTLViolationError(IACMException):
    """Flight Time Limitation violation"""
    def __init__(self, detail: str):
        super().__init__(status.HTTP_409_CONFLICT, detail, "FTL_VIOLATION")


class CrewBlockedError(IACMException):
    """Crew member is blocked and cannot be assigned"""
    def __init__(self, crew_name: str, reason: str = None):
        detail = f"Crew member '{crew_name}' is blocked"
        if reason:
            detail += f": {reason}"
        super().__init__(status.HTTP_409_CONFLICT, detail, "CREW_BLOCKED")


class DocumentExpiredError(IACMException):
    """Required document is expired"""
    def __init__(self, document_type: str):
        super().__init__(status.HTTP_409_CONFLICT, f"Required document '{document_type}' is expired", "DOCUMENT_EXPIRED")

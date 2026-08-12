from .emergency_lifecycle import emergency_context


def emergency_mode(request):
    return {"emergency_mode": emergency_context()}

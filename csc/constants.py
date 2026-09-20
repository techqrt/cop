class Constants:
    """Shared user-facing string constants, ported from pms/constants.py
    (docs/pms-reference-analysis.md §7/§8)."""

    validation_error = 'Validation Error'
    auth_error = 'Authentication Error'
    server_error = 'Server Error'
    access_token_expired = 'Expired access token'
    refresh_token_invalid = 'Invalid refresh token'
    invalid_header = 'Invalid authorization header'
    invalid_access_token = 'Invalid access token'
    invalid_credentials = 'Invalid email or password'
    auth_success = 'Authentication successful'
    email_not_unique = 'The email is already registered'
    page_num_exceeded = 'The given page number is greater than the maximum available limit'
    forbidden_access = 'Forbidden access'

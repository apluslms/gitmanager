from hashlib import md5


def detect_user(request):
    '''
    Tries to detect userid from the request.
    '''
    user = request.GET.get("user", None)

    # Check submission_url for A+ userid.
    if not user and "submission_url" in request.GET:
        parts = request.GET["submission_url"].partition("/exercise/rest/")[2].split("/")
        if len(parts) > 4 and parts[2] == "students":
            return parts[3]

    if user:
        return user.strip()
    return user


def make_hash(secret, user):
    '''
    Creates a hash key for a user.
    '''
    if user is not None:
        coded = secret + user
        return md5(coded.encode('ascii', 'ignore')).hexdigest()
    return None

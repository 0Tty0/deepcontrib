FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

# The test runner invokes pytest explicitly.  Keeping the image entrypoint empty
# prevents a repository-controlled command from changing the sandbox contract.
RUN python -m pip install --no-cache-dir --disable-pip-version-check \
    pytest==8.4.2 \
    iniconfig==2.3.0 \
    packaging==26.3 \
    pluggy==1.6.0 \
    pygments==2.21.0

WORKDIR /workspace

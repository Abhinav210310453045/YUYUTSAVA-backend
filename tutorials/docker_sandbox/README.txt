Build the sandbox image from the repo root:

  docker build -t deepagent-sandbox:local -f tutorials/docker_sandbox/Dockerfile .

Then run goog with --execution docker --docker-image deepagent-sandbox:local (see tutorials/README.md).

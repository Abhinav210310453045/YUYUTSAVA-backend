Build the sandbox image from the repo root:

  docker build -t deepagent-sandbox:local -f yuyutsava/docker_sandbox/Dockerfile .

deepagent-sandbox:local is the image `--execution docker` uses by default
(override with --docker-image or YUYUTSAVA_DOCKER_IMAGE):

  yuyutsava --execution docker "your task"

The backend that runs tools inside the container is backend.py in this package.

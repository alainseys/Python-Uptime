FROM python:3.11.13-bullseye

# ----------------------------------------
# Set workspace
# ----------------------------------------
ENV WORKSPACE_BASE_PATH=/workspaces/ansible
WORKDIR ${WORKSPACE_BASE_PATH}

# ----------------------------------------
# Install dependencies
# ----------------------------------------
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        sshpass \
        apt-transport-https \
        ca-certificates \
        curl \
        gnupg-agent \
        less \
        software-properties-common \
        openssh-client \
        git && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ----------------------------------------
# Install Docker CLI for Molecule
# ----------------------------------------
RUN curl -fsSL https://download.docker.com/linux/debian/gpg | apt-key add - && \
    add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/debian $(lsb_release -cs) stable" && \
    apt-get update && apt-get install -y docker-ce-cli && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# ----------------------------------------
# Install Python packages
# ----------------------------------------
RUN python3 -m pip install --upgrade pip
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN pip install ansible ansible-lint ansible-dev-tools ansible-builder ansible-creator ansible-navigator \
    pywinrm molecule pyVmomi requests docker netmiko yamllint dnspython pytest-molecule

# ----------------------------------------
# Git configuration
# ----------------------------------------
RUN git config --global user.email "alain.seys@outlook.com" && \
    git config --global user.name "Alain Seys"

# ----------------------------------------
# Copy private SSH key and configure SSH
# ----------------------------------------
COPY ../private_key /root/.ssh/id_ed25519
RUN chmod 600 /root/.ssh/id_ed25519

# ----------------------------------------
# Optional: allow SSHD access
# ----------------------------------------
RUN echo 'SSHD: ALL' >> /etc/hosts.allow

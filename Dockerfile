FROM python:3.10

RUN apt install wget -y

RUN wget https://packages.microsoft.com/config/debian/10/packages-microsoft-prod.deb \
    -O packages-microsoft-prod.deb \
    && dpkg -i packages-microsoft-prod.deb \
    && rm packages-microsoft-prod.deb

RUN apt-get update && apt-get install -y dotnet-sdk-8.0 \
    && apt-get install libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libdrm2 libxkbcommon0 libasound2 -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN useradd -m user
RUN pip3 install -r requirements.txt && \
    rm -f requirements.txt
USER user
#RUN playwright install
USER root
COPY eruditus /eruditus
COPY .git/refs/heads/master /eruditus/.revision

WORKDIR /eruditus

RUN chown -R user:user .
COPY .ssh /home/user/.ssh/
COPY chat_exporter /usr/bin/
RUN chmod a+x /usr/bin/chat_exporter && \
    chown -R user:user /home/user

USER user
# Prevent caching the subsequent "git clone" layer.
# https://github.com/moby/moby/issues/1996#issuecomment-1152463036
#ADD http://worldclockapi.com/api/json/utc/now /etc/builddate
RUN git clone https://github.com/hfz1337/DiscordChatExporter ~/DiscordChatExporter

ARG CHATLOGS_REPO=git@github.com:kunshim/deadsec_ctfbot.git

RUN git clone --depth=1 $CHATLOGS_REPO ~/chatlogs
RUN git config --global user.email "eruditus@localhost" && \
    git config --global user.name "eruditus"

ENTRYPOINT ["python3", "-u", "eruditus.py"]



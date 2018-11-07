FROM nrel/openstudio:2.7.0
RUN sudo apt-get update && \
    sudo apt-get install -y build-essential libpq-dev libssl-dev openssl libffi-dev zlib1g-dev wget
RUN wget https://www.python.org/ftp/python/3.6.7/Python-3.6.7.tgz && \
    tar -xvf Python-3.6.7.tgz && \
    cd Python-3.6.7 && \
    sudo ./configure --enable-optimizations && \
    sudo make && \
    sudo make install && \
    cd .. && \
    rm -rf Python-3.6.7
COPY . /buildstock-batch/
RUN sudo pip3 install --upgrade pip && sudo pip3 install /buildstock-batch
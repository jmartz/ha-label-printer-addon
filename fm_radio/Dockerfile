# FM Radio add-on: RTL-SDR -> live FM audio (Icecast MP3) + RDS -> MQTT.
FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive

# Toolchain + runtime deps. icecast2 streams the audio; ffmpeg encodes MP3;
# libsndfile is a redsea dep; libusb for the SDR. autoconf/automake/cmake/meson
# to build the three things Debian doesn't package for us.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git build-essential cmake pkg-config ca-certificates autoconf automake \
        libusb-1.0-0-dev meson ninja-build libsndfile1-dev \
        ffmpeg icecast2 \
    && rm -rf /var/lib/apt/lists/*

# RTL-SDR Blog driver fork — the V4 (R828D) needs this; stock librtlsdr mistunes it.
RUN git clone --depth 1 https://github.com/rtlsdrblog/rtl-sdr-blog /tmp/rtl \
    && cmake -S /tmp/rtl -B /tmp/rtl/b -DINSTALL_UDEV_RULES=ON -DDETACH_KERNEL_DRIVER=ON \
    && cmake --build /tmp/rtl/b -j"$(nproc)" \
    && cmake --install /tmp/rtl/b \
    && ldconfig && rm -rf /tmp/rtl

# liquid-dsp — redsea's DSP dependency (not reliably in Debian).
RUN git clone --depth 1 https://github.com/jgaeddert/liquid-dsp /tmp/liq \
    && cd /tmp/liq && ./bootstrap.sh && ./configure && make -j"$(nproc)" \
    && make install && ldconfig && rm -rf /tmp/liq

# redsea — FM RDS decoder (not packaged; build with meson).
RUN git clone --depth 1 https://github.com/windytan/redsea /tmp/red \
    && meson setup /tmp/red/b /tmp/red \
    && meson compile -C /tmp/red/b \
    && meson install -C /tmp/red/b \
    && ldconfig && rm -rf /tmp/red

RUN pip install --no-cache-dir "paho-mqtt<2.0"

WORKDIR /app
COPY fm_radio_service.py icecast.xml /app/
CMD ["python", "fm_radio_service.py"]

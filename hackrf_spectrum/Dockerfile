# Multi-arch Python base (Pi/aarch64 + x86/amd64), same pattern as label_printer.
FROM python:3.12-slim

# hackrf = the HackRF One CLI tools (hackrf_sweep, hackrf_info) + libhackrf +
# libusb. This is what actually talks to the radio.
RUN apt-get update \
    && apt-get install -y --no-install-recommends hackrf \
    && rm -rf /var/lib/apt/lists/*

# numpy/matplotlib render the waterfall PNG; paho-mqtt (<2.0 for the v1 callback
# API this service uses) publishes the sensors. All install from prebuilt wheels.
RUN pip install --no-cache-dir numpy matplotlib "paho-mqtt<2.0"

ENV MPLCONFIGDIR=/tmp/mplconfig
WORKDIR /app
COPY spectrum_service.py /app/
CMD ["python", "spectrum_service.py"]

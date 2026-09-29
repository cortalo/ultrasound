# ultrasound

Measurement data and analysis code for the ultrasound link.

| directory | what is in it |
|---|---|
| [`downlink/`](downlink/) | FSK downlink bit-error-rate measurements: the scope captures, the decoder that scores them, and the results |
| [`uplink/`](uplink/) | Backscatter uplink bit-error-rate measurement: 10<sup>7</sup> bit decisions, two raw scope chunks, and the summaries |

## Getting the data and running it

```sh
git clone git@github.com:cortalo/ultrasound.git
cd ultrasound/downlink
./get_captures.sh      # 4.9 GB of captures, downloaded and checksummed
./run_ber.sh           # decode every capture, print bits and errors per rate

cd ../uplink
./get_captures.sh      # 374 MB of bit decisions and scope chunks, checksummed
```


"""Run on an adequate CPU worker; default stage acquires audio only."""

from clipforge.audio_first import main

if __name__ == "__main__":
    raise SystemExit(main())

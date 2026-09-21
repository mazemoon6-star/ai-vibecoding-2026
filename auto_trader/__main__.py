"""Run the paper trader with ``python -m auto_trader``."""

import uvicorn


if __name__ == "__main__":
    uvicorn.run("auto_trader.app:app", host="127.0.0.1", port=8000, reload=False)

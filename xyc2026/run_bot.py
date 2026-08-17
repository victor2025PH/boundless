# Run Bot (start API first)
import asyncio
import sys
from bot.main import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")
        sys.exit(0)

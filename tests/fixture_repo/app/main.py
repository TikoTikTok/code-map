"""Entry point."""
import os

from app.util import Greeter, helper


def main():
    g = Greeter(os.environ.get("NAME", "x"))
    print(g.greet())
    return helper(1)


if __name__ == "__main__":
    main()

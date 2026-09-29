"""Entry point for the bundled app. (The package's own __main__ uses relative imports, which PyInstaller cannot start from.)"""
from nextrunner.cli import main

if __name__ == "__main__":
    main()

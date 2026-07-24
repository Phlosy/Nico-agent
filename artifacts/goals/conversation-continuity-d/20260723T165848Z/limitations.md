# CC-D Limitations

- Actions are in-memory facts only; U5 owns persistence.
- The live dispatcher does not call the new parser; U6 owns activation.
- `ask_user` is not advertised or executable.
- Legacy plain-text final compatibility remains enabled.
- No credentialed external model behavior is claimed.

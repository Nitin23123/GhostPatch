from notes.tags import add_tag


class Note:
    def __init__(self, text):
        self.text = text
        self.tags = add_tag("new")

    def tag(self, name):
        self.tags = add_tag(name, self.tags)
        return self

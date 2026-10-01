import menu


class FakeQuestion:
    def __init__(self, answer):
        self.answer = answer

    def ask(self):
        return self.answer


def picking(monkeypatch, pick):
    """Make the arrow menu answer with the value of the choice ``pick`` selects."""
    def select(title, choices, instruction):
        return FakeQuestion(pick(choices).value)
    monkeypatch.setattr(menu, "interactive", lambda: True)
    monkeypatch.setattr(menu.questionary, "select", select)


def test_back_in_arrow_menu_returns_none(monkeypatch):
    picking(monkeypatch, lambda choices: choices[-1])
    assert menu.choose("Мои поиски:", ["a", "b"]) is None


def test_option_in_arrow_menu_returns_its_index(monkeypatch):
    picking(monkeypatch, lambda choices: choices[1])
    assert menu.choose("Мои поиски:", ["a", "b"]) == 1


def test_ctrl_c_in_arrow_menu_counts_as_back(monkeypatch):
    monkeypatch.setattr(menu, "interactive", lambda: True)
    monkeypatch.setattr(menu.questionary, "select", lambda *a, **k: FakeQuestion(None))
    assert menu.choose("x", ["a"]) is None

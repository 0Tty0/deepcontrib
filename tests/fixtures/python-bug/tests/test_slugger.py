from src.slugger import slugify


def test_slugify_collapses_repeated_spaces() -> None:
    assert slugify("Hello,  World!") == "hello,-world!"

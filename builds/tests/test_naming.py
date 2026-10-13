"""Phase 4 tests: image naming (README section 6)."""
from integrations import naming


def test_image_repository_folds_owner_and_name():
    assert (
        naming.image_repository("acme", "autodeploy", 17, "mates")
        == "acme/autodeploy-17-mates"
    )


def test_image_tag_is_first_seven_of_sha():
    assert naming.image_tag("a82f91c0123456789") == "a82f91c"
    assert len(naming.image_tag("a" * 40)) == 7

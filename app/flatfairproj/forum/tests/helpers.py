from forum.models import Post


def makeQuestion(author, title="How long does resale take?", topic="selling", **fields):
    return Post.objects.create(
        author=author, title=title, topic=topic, body="We just listed our flat.", **fields
    )


def makeReply(question, author, body="About four months for us.", **fields):
    return Post.objects.create(author=author, question=question, body=body, **fields)

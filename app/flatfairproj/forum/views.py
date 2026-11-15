from urllib.parse import urlencode
from datetime import timedelta
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Exists, Min, OuterRef, Prefetch, Q
from django.http import Http404
from django.contrib.auth.models import User
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from adminpanel.access import staffPage
from adminpanel.views import page, audit
from storefront.notices import notify
from .models import Post, Vote, Report, TOPICS, REASONS

SORTS = [("new", "Newest"), ("votes", "Most votes")]
REPLIES_PER_PAGE = 20
STAFF_NOTE = "Staff accounts can read the community but cannot post."


class QuestionForm(forms.ModelForm):
    topic = forms.ChoiceField(
        label="Topic",
        choices=[("", "Choose…")] + TOPICS,
        error_messages={
            "required": "Choose a topic.",
            "invalid_choice": "Choose a topic from the list.",
        },
    )
    title = forms.CharField(
        label="Your question",
        max_length=150,
        widget=forms.TextInput(
            attrs={
                "maxlength": 150,
                "placeholder": "e.g. How long did your HDB resale take?",
            }
        ),
        error_messages={
            "required": "Write your question.",
            "max_length": "Keep your question to 150 characters or fewer.",
        },
    )

    class Meta:
        model = Post
        fields = ["topic", "title", "body"]
        labels = {"body": "More details"}
        widgets = {"body": forms.Textarea(attrs={"rows": 5, "maxlength": 3000})}
        error_messages = {
            "body": {
                "required": "Add a few details.",
                "max_length": "Keep the details to 3,000 characters or fewer.",
            },
        }


class ReplyForm(forms.ModelForm):
    class Meta:
        model = Post
        fields = ["body"]
        labels = {"body": "Your reply"}
        widgets = {"body": forms.Textarea(attrs={"rows": 4, "maxlength": 3000})}
        error_messages = {
            "body": {
                "required": "Write your reply first.",
                "max_length": "Keep your reply to 3,000 characters or fewer.",
            },
        }


def canPost(user):
    return user.is_authenticated and not user.is_staff


def live_posts():
    return Post.objects.filter(hidden=False).exclude(question__hidden=True)


def with_votes(posts, user):
    posts = posts.select_related("author").annotate(voteCount=Count("votes"))

    if user.is_authenticated:
        posts = posts.annotate(
            myVote=Exists(Vote.objects.filter(post=OuterRef("pk"), user=user))
        )

    return posts


def userFame(request):
    if hasattr(request, "forumReputation"):
        return request.forumReputation

    now = timezone.now()

    firstDay = timezone.localtime(now).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    members = {}

    stats = (
        live_posts()
        .values("author_id", "author__first_name")
        .annotate(
            questionCount=Count("pk", filter=Q(question=None), distinct=True),
            replyCount=Count("pk", filter=Q(question__isnull=False), distinct=True),
            voteCount=Count("votes", distinct=True),
            replyVotes=Count("votes", filter=Q(question__isnull=False), distinct=True),
            monthReplies=Count(
                "pk",
                filter=Q(question__isnull=False, createdAt__gte=firstDay),
                distinct=True,
            ),
            monthVotes=Count("votes", filter=Q(createdAt__gte=firstDay), distinct=True),
            weekPosts=Count(
                "pk", filter=Q(createdAt__gte=now - timedelta(days=7)), distinct=True
            ),
            monthPosts=Count(
                "pk", filter=Q(createdAt__gte=now - timedelta(days=30)), distinct=True
            ),
            firstPost=Min("createdAt"),
        )
    )

    for row in stats:
        row["id"] = row.pop("author_id")
        row["name"] = row.pop("author__first_name") or "A member"



        row["initial"] = row["name"][0].upper()
        row["tone"] = ("sky", "mint", "lilac")[row["id"] % 3]
        row["points"] = row["voteCount"] + 2 * row["replyCount"]
        row["monthPoints"] = row["monthVotes"] + 2 * row["monthReplies"]
        row["badges"] = []
        members[row["id"]] = row

    monthly = sum(row["monthPoints"] > 0 for row in members.values()) >= 3

    if monthly:
        score = "monthPoints"
    else:
        score = "points"

    ranked = sorted(
        members.values(), key=lambda row: (-row[score], -row["points"], row["id"])
    )

    leaders = [row for row in ranked if row[score] > 0][:3]

    for row in members.values():
        row["rankPoints"] = row[score]

        if row in leaders:
            if monthly:
                period = "made this month"
            else:
                period = "overall"

            row["badges"].append(
                {
                    "label": "Top contributor",
                    "title": f"Top 3 by points on visible posts {period}. Each vote earns 1 point and each reply earns 2.",
                    "kind": "top",
                }
            )

        if row["replyVotes"] >= 3:
            row["badges"].append(
                {
                    "label": "Helpful",
                    "title": "Received at least 3 votes on replies.",
                    "kind": "helpful",
                }
            )

        if row["firstPost"] >= now - timedelta(days=14):
            row["badges"].append(
                {
                    "label": "New member",
                    "title": "First visible community post was within the last 14 days.",
                    "kind": "new",
                }
            )

    if any(row["weekPosts"] for row in members.values()):
        activePeriod = "weekPosts"
    else:
        activePeriod = "monthPosts"



    active = sorted(
        members.values(),
        key=lambda row: (-row[activePeriod], -row["points"], row["id"]),
    )

    for row in active:
        row["activeCount"] = row[activePeriod]


    request.forumReputation = {
        "members": members,
        "topContributors": [row for row in ranked if row[score] > 0][:5],
        "contributorPeriod": "Posts made this month" if monthly else "Overall",
        "activeMembers": [row for row in active if row["activeCount"] > 0][:5],
        "activePeriod": (
            "Last 7 days" if activePeriod == "weekPosts" else "Last 30 days"
        ),
    }

    return request.forumReputation


def showPosts(posts, reputation):
    now = timezone.now()



    for post in posts:
        post.member = reputation["members"].get(post.author_id)



        seconds = max(0, int((now - post.createdAt).total_seconds()))
        post.relativeDate = "Just now"

        for unit, span in (
            ("year", 31536000),
            ("month", 2592000),
            ("week", 604800),
            ("day", 86400),
            ("hour", 3600),
            ("minute", 60),
        ):
            if seconds >= span:
                count = seconds // span

                if count != 1:
                    plural = "s"
                else:
                    plural = ""

                post.relativeDate = f"{count} {unit}{plural} ago"

                break


def listUrl(topic, sort, query):
    params = {}

    if topic:
        params["topic"] = topic

    if sort != "new":
        params["sort"] = sort

    if query:
        params["q"] = query

    return "?" + urlencode(params)


def community(request):
    topic = request.GET.get("topic", "")

    if topic not in dict(TOPICS):
        topic = ""

    sort = request.GET.get("sort")



    if sort not in dict(SORTS):
        sort = "new"

    query = request.GET.get("q", "").strip()[:100]

    asks = Post.objects.filter(question=None, hidden=False).select_related("author")

    asks = asks.annotate(
        voteCount=Count("votes", distinct=True),
        replyCount=Count("replies", filter=Q(replies__hidden=False), distinct=True),
        answered=Exists(
            Post.objects.filter(
                question=OuterRef("pk"), hidden=False, votes__isnull=False
            )
        ),
    )

    if topic:
        asks = asks.filter(topic=topic)

    if query:
        asks = asks.filter(title__icontains=query)

    if sort == "votes":
        asks = asks.order_by("-voteCount", "-createdAt", "-pk")
    else:
        asks = asks.order_by("-createdAt", "-pk")

    chips = [
        {"label": "All topics", "url": listUrl("", sort, query), "on": topic == ""}
    ]



    for value, label in TOPICS:
        chips.append(
            {"label": label, "url": listUrl(value, sort, query), "on": topic == value}
        )

    fame = userFame(request)



    rows = Paginator(asks, 10).get_page(request.GET.get("page"))
    showPosts(rows, fame)

    perTopic = dict(
        Post.objects.filter(question=None, hidden=False)
        .values("topic")
        .annotate(total=Count("pk"))
        .values_list("topic", "total")
    )

    context = {
        **fame,
        "rows": rows,
        "popularTopics": sorted(
            [
                {
                    "label": label,
                    "count": perTopic.get(value, 0),
                    "url": listUrl(value, "new", ""),
                }
                for value, label in TOPICS
            ],
            key=lambda row: -row["count"],
        ),
        "chips": chips,
        "sorts": SORTS,
        "sort": sort,
        "topic": topic,
        "query": query,
    }

    return render(request, "forum/community.html", context)


def question(request, pk):
    item = get_object_or_404(
        with_votes(Post.objects.filter(question=None), request.user), pk=pk
    )

    if item.hidden and not request.user.is_staff:
        raise Http404

    form = ReplyForm()

    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect_to_login(request.path)

        if not canPost(request.user):
            messages.error(request, STAFF_NOTE)

            return redirect(item.url())

        form = ReplyForm(request.POST)

        if form.is_valid():
            reply = form.save(commit=False)
            reply.author = request.user
            reply.question = item



            reply.save()

            if item.author_id != request.user.pk:
                name = request.user.first_name or "A member"

                notify(
                    item.author_id,
                    "community_reply",
                    f"{name} replied to your question: {item.title}",
                    reply.url(),
                )

            messages.success(request, "Your reply is posted.")


            return redirect(reply.url())

    replies = with_votes(item.replies.filter(hidden=False), request.user).order_by(
        "-voteCount", "createdAt", "pk"
    )

    bestId = replies.filter(voteCount__gt=0).values_list("pk", flat=True).first()
    replies = Paginator(replies, REPLIES_PER_PAGE).get_page(replyPage(request, item))
    fame = userFame(request)
    showPosts([item], fame)
    showPosts(replies, fame)

    context = {
        "item": item,
        "replies": replies,
        "topAnswerId": bestId,
        "form": form,
        "canPost": canPost(request.user),
        "reasons": REASONS,
    }

    return render(request, "forum/question.html", context)


def replyPage(request, item):
    if request.GET.get("page"):
        return request.GET["page"]



    postId = request.GET.get("post", "")

    if len(postId) > 19 or not postId.isdecimal():
        return 1

    replies = item.replies.filter(hidden=False).annotate(voteCount=Count("votes"))
    target = replies.filter(pk=postId).first()

    if target is None:
        return 1

    earlier = replies.filter(
        Q(voteCount__gt=target.voteCount)
        | Q(voteCount=target.voteCount, createdAt__lt=target.createdAt)
        | Q(voteCount=target.voteCount, createdAt=target.createdAt, pk__lt=target.pk)
    )

    return earlier.count() // REPLIES_PER_PAGE + 1


@login_required
def ask(request):
    if not canPost(request.user):
        messages.error(request, STAFF_NOTE)

        return redirect("community")

    form = QuestionForm(
        request.POST or None, initial={"topic": request.GET.get("topic")}
    )

    if request.method == "POST" and form.is_valid():
        item = form.save(commit=False)



        item.author = request.user
        item.save()
        messages.success(request, "Your question is posted.")

        return redirect(item.url())

    return render(request, "forum/ask.html", {"form": form})


@login_required
def edit(request, pk):
    post = get_object_or_404(live_posts().filter(author=request.user), pk=pk)

    if not canPost(request.user):
        messages.error(request, STAFF_NOTE)

        return redirect(post.url())

    if post.question_id:
        form = ReplyForm(request.POST or None, instance=post)
    else:
        form = QuestionForm(request.POST or None, instance=post)

    if request.method == "POST" and form.is_valid():
        if form.has_changed():
            post = form.save(commit=False)
            post.editedAt = timezone.now()
            post.save()
            messages.success(request, "Your changes are saved.")



        return redirect(post.url())

    return render(request, "forum/edit.html", {"form": form, "post": post})


def member(request, pk):
    person = get_object_or_404(User, pk=pk)

    asks = Post.objects.filter(author=person, question=None, hidden=False).order_by(
        "-createdAt", "-pk"
    )

    replies = (
        live_posts()
        .filter(author=person, question__isnull=False)
        .select_related("question")
    )

    replies = replies.order_by("-createdAt", "-pk")
    fame = userFame(request)

    stats = fame["members"].get(
        person.pk,
        {
            "name": person.first_name or "A member",
            "initial": (person.first_name or "A")[0].upper(),
            "tone": "sky",
            "points": 0,
            "badges": [],
            "voteCount": 0,
            "questionCount": 0,
            "replyCount": 0,
        },
    )

    askNum = stats["questionCount"]
    replyNum = stats["replyCount"]

    if person.pk != request.user.pk and askNum == 0 and replyNum == 0:
        raise Http404

    newPosts = (
        live_posts()
        .filter(author=person)
        .select_related("question")
        .order_by("-createdAt", "-pk")[:20]
    )

    showPosts(newPosts, fame)


    context = {
        "person": person,
        "memberStats": stats,
        "recentPosts": newPosts,
        "questions": asks[:20],
        "replies": replies[:20],
        "questionCount": askNum,
        "replyCount": replyNum,
        "voteCount": stats["voteCount"],
    }


    return render(request, "forum/member.html", context)


@login_required
@require_POST
def vote(request, pk):
    post = get_object_or_404(live_posts(), pk=pk)

    if not canPost(request.user) or post.author_id == request.user.pk:
        messages.error(request, "You cannot vote on this post.")

        return redirect(post.url())

    mine, created = Vote.objects.get_or_create(user=request.user, post=post)

    if not created:
        mine.delete()

    return redirect(post.url())


@login_required
@require_POST
def report(request, pk):
    post = get_object_or_404(live_posts(), pk=pk)



    reason = request.POST.get("reason")

    if not canPost(request.user) or post.author_id == request.user.pk:
        messages.error(request, "You cannot report this post.")
    elif reason not in dict(REASONS):
        messages.error(request, "Choose what is wrong with the post.")
    else:
        Report.objects.get_or_create(
            reporter=request.user, post=post, defaults={"reason": reason}
        )



        messages.success(request, "Thanks. Our team will check this post.")

    return redirect(post.url())


@staffPage("admin_community")
def moderation(request):
    show = request.GET.get("show")

    if show != "hidden":
        show = "reported"

    reports = (
        Report.objects.filter(resolved=False)
        .select_related("reporter")
        .order_by("createdAt")
    )

    rows = Post.objects.select_related("author", "question").prefetch_related(
        Prefetch("reports", queryset=reports, to_attr="openReports")
    )


    if show == "hidden":
        rows = rows.filter(hidden=True)
    else:
        rows = rows.annotate(
            waiting=Count("reports", filter=Q(reports__resolved=False))
        ).filter(hidden=False, waiting__gt=0)

    context = {
        "rows": rows.order_by("-createdAt", "-pk"),
        "show": show,
        "counts": {
            "reported": Post.objects.filter(hidden=False, reports__resolved=False)
            .distinct()
            .count(),
            "hidden": Post.objects.filter(hidden=True).count(),
        },
    }

    return page(request, "forum/moderation.html", context)


@staffPage("admin_community")
def judge_post(request, pk):
    reports = (
        Report.objects.filter(resolved=False)
        .select_related("reporter")
        .order_by("createdAt")
    )


    post = (
        Post.objects.select_related("author", "question")
        .prefetch_related(Prefetch("reports", queryset=reports, to_attr="openReports"))
        .filter(pk=pk)
        .first()
    )

    if not post:
        messages.error(request, "That post no longer exists.")

        return redirect("admin_community")

    return page(
        request, "forum/moderation_post.html", {"p": post}, section="admin_community"
    )


@staffPage("admin_community")
@require_POST
@transaction.atomic
def moderate(request, pk):
    actions = {
        "hide": ("community post hidden", "The post is hidden from the community."),
        "show": ("community post shown", "The post is shown in the community again."),
        "keep": (
            "community report closed",
            "The reports are closed and the post stays up.",
        ),
    }

    post = get_object_or_404(Post.objects.select_for_update(), pk=pk)
    action = request.POST.get("action")
    reason = request.POST.get("reason", "").strip()[:200]
    back = reverse("admin_community")

    if post.hidden:
        back += "?show=hidden"



    if action not in actions:
        return redirect(back)

    if action != "keep" and not reason:
        messages.error(request, "Write a short reason first.")

        return redirect(back)

    if action == "hide":
        post.hidden = True
        post.hiddenReason = reason


        post.save(update_fields=["hidden", "hiddenReason"])

        from storefront.notices import notify

        if post.title:
            what = f"your question “{post.title[:60]}”"
        else:
            what = "your reply"

        notify(
            post.author_id,
            "post_hidden",
            f"Our team hid {what} in the community. Reason: {reason}",
            reverse("community"),
        )

    if action == "show":
        post.hidden = False
        post.hiddenReason = ""



        post.save(update_fields=["hidden", "hiddenReason"])

    post.reports.filter(resolved=False).update(resolved=True)
    label, note = actions[action]
    audit(request, label, f"Community post #{post.pk}", reason or "Nothing wrong found")
    messages.success(request, note)

    return redirect(back)

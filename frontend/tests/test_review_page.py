"""The review asks for stars, not for identifiers."""

from django.template.loader import render_to_string
from django.test import SimpleTestCase


def render_review(**overrides):
    context = {
        "form": type("F", (), {"booking": type("B", (), {"value": lambda self: "8a1f2c44-1111-4a11-9b22-33cc44dd55ee"})(),
                               "rating": type("R", (), {"value": lambda self: None, "errors": []})(),
                               "comment": type("C", (), {"value": lambda self: "", "errors": []})()})(),
        "star_values": [1, 2, 3, 4, 5],
        "csrf_token": "test-only",
        "nav_role": "customer",
        "role": "customer",
        "tour_role": "customer",
        "tour_user_key": "guest",
        "platform_manager": False,
        "request": type("Req", (), {"path": "/reviews/new/"})(),
    }
    context.update(overrides)
    return render_to_string("reviews/new.html", context)


def render_thanks(**overrides):
    context = {
        "next_url": "/booking/8a1f2c44-1111-4a11-9b22-33cc44dd55ee/",
        "csrf_token": "test-only",
        "nav_role": "customer",
        "role": "customer",
        "tour_role": "customer",
        "tour_user_key": "guest",
        "platform_manager": False,
        "request": type("Req", (), {"path": "/reviews/thanks/"})(),
    }
    context.update(overrides)
    return render_to_string("reviews/thanks.html", context)


class ReviewPageTests(SimpleTestCase):
    def test_the_rating_is_five_stars(self):
        html = render_review()
        for value in (1, 2, 3, 4, 5):
            self.assertIn(f'name="rating" id="rating-{value}" value="{value}"', html)
        self.assertIn("How was the work?", html)

    def test_nothing_about_the_artisan_is_shown(self):
        html = render_review()
        self.assertNotIn('name="artisan"', html)
        self.assertNotIn('name="artisan_id"', html)
        self.assertNotIn("Artisan ID", html)

    def test_the_comment_is_optional(self):
        html = render_review()
        marker = html.index('name="comment"')
        self.assertNotIn("required", html[marker:marker + 200])
        self.assertIn("optional", html)

    def test_the_stars_read_left_to_right(self):
        html = render_review()
        order = [html.index(f'id="rating-{value}"') for value in (1, 2, 3, 4, 5)]
        self.assertEqual(order, sorted(order))

    def test_the_improvement_note_is_a_popup_with_a_cancel(self):
        html = render_thanks()
        self.assertIn("One thing we could do better?", html)
        self.assertIn('data-improve-pop role="dialog"', html)
        self.assertIn("data-improve-cancel", html)
        self.assertIn(">Cancel<", html)
        self.assertIn("Thanks for your review!", html)
        self.assertNotIn("data-improve-card", render_review())

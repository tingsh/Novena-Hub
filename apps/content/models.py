from django.db import models
from modelcluster.fields import ParentalKey
from wagtail import blocks
from wagtail.admin.panels import FieldPanel, InlinePanel
from wagtail.fields import RichTextField, StreamField
from wagtail.images.blocks import ImageChooserBlock
from wagtail.models import Orderable, Page
from wagtail.search import index

from apps.content.blocks import (
    AISpotlightBlock,
    CaptionBlock,
    FAQAccordionBlock,
    FeatureGridBlock,
    FeatureSectionBlock,
    FinalCTABlock,
    HeroBlock,
    LeadCaptureBlock,
    MarketingJourneyBlock,
    MarketingMediaBlock,
    MarketingPhotoGridBlock,
    MarketingSolutionBlock,
    MetricBandBlock,
    PlatformCapabilitiesBlock,
    PricingComparisonBlock,
    PricingTiersBlock,
    ProcessBlock,
    ProductVisualBlock,
    ProofStripBlock,
    SolutionsSectionBlock,
    TrustedByBlock,
    VerticalSolutionCardsBlock,
)


def _get_default_block_types():
    return [
        ("paragraph", blocks.RichTextBlock()),
        ("image", ImageChooserBlock()),
        ("caption", CaptionBlock()),
        ("html", blocks.RawHTMLBlock()),
    ]


class BaseContentPage(Page):
    social_image = models.ImageField(null=True, blank=True, help_text="The image to use in social sharing metadata.")
    promote_panels = Page.promote_panels + [
        FieldPanel("social_image"),
    ]

    def get_social_image_url(self):
        if self.social_image:
            return self.social_image.url
        return ""

    class Meta:
        abstract = True


class ContentPage(BaseContentPage):
    """
    A page of generic content.
    """

    body = StreamField(_get_default_block_types())
    content_panels = Page.content_panels + [
        FieldPanel("body", classname="full"),
    ]


class BlogIndexPage(BaseContentPage):
    """
    Index page for a blog
    """

    intro = RichTextField(blank=True)
    content_panels = Page.content_panels + [FieldPanel("intro", classname="full")]

    def get_ordered_blog_posts(self):
        return self.get_children().live().specific().order_by("-first_published_at")


class BlogPage(BaseContentPage):
    """
    A single blog post
    """

    date = models.DateField("Post date")
    intro = models.CharField(max_length=250)
    body = StreamField(_get_default_block_types())

    search_fields = Page.search_fields + [
        index.SearchField("intro"),
        index.SearchField("body"),
    ]

    content_panels = Page.content_panels + [
        FieldPanel("date"),
        FieldPanel("intro"),
        FieldPanel("body", classname="full"),
        InlinePanel("gallery_images", label="Gallery images"),
    ]

    @property
    def main_image(self):
        gallery_item = self.gallery_images.first()
        if gallery_item:
            return gallery_item.image
        else:
            return None


class BlogPageGalleryImage(Orderable):
    page = ParentalKey(BlogPage, on_delete=models.CASCADE, related_name="gallery_images")
    image = models.ForeignKey("wagtailimages.Image", on_delete=models.CASCADE, related_name="+")
    caption = models.CharField(blank=True, max_length=250)

    panels = [
        FieldPanel("image"),
        FieldPanel("caption"),
    ]


class HomePage(BaseContentPage):
    """
    The homepage of the Novena site.
    """

    body = StreamField(_get_default_block_types(), blank=True)
    content_panels = Page.content_panels + [
        FieldPanel("body", classname="full"),
    ]

    def serve(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            from django.http import HttpResponseRedirect
            from django.urls import reverse

            team = request.team
            if team:
                return HttpResponseRedirect(reverse("web_team:home", args=[team.slug]))
            else:
                from django.contrib import messages
                from django.utils.translation import gettext_lazy as _

                from apps.teams.helpers import get_open_invitations_for_user

                if (open_invitations := get_open_invitations_for_user(request.user)) and len(open_invitations) > 1:
                    invitation = open_invitations[0]
                    return HttpResponseRedirect(reverse("teams:accept_invitation", args=[invitation["id"]]))

                messages.info(
                    request,
                    _(
                        "Teams are enabled but you have no teams. Create a team below to access the rest "
                        "of the dashboard."
                    ),
                )
                return HttpResponseRedirect(reverse("teams:manage_teams"))
        return super().serve(request, *args, **kwargs)


class NovenaHomePage(BaseContentPage):
    hero_tagline = models.CharField(max_length=255, default="Industrial IoT for SME operations", blank=True)
    hero_title = models.CharField(
        max_length=255, default="Connect equipment. See operations clearly. Act faster.", blank=True
    )
    hero_subtitle = models.TextField(
        default=(
            "Novena Platform connects meters, PLCs, sensors, and industrial assets to live dashboards, "
            "alerts, AI insights, and operational workflows built for lean teams."
        ),
        blank=True,
    )
    hero_cta_text = models.CharField(max_length=100, default="Sign up", blank=True)
    hero_cta_url = models.CharField(max_length=255, default="/accounts/signup/", blank=True)
    hero_secondary_cta_text = models.CharField(max_length=100, default="Explore Platform", blank=True)
    hero_secondary_cta_url = models.CharField(max_length=255, default="#features", blank=True)
    hero_image = models.ForeignKey(
        "wagtailimages.Image", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    hero_image_alt = models.CharField(max_length=255, blank=True)
    show_terminal_simulator = models.BooleanField(
        default=True, help_text="Show the interactive terminal simulator on the right side of the hero."
    )

    body = StreamField(
        [
            ("trusted_by", TrustedByBlock()),
            ("marketing_media", MarketingMediaBlock()),
            ("marketing_photo_grid", MarketingPhotoGridBlock()),
            ("marketing_journey", MarketingJourneyBlock()),
            ("marketing_solution", MarketingSolutionBlock()),
            ("metric_band", MetricBandBlock()),
            ("product_visual", ProductVisualBlock()),
            ("process", ProcessBlock()),
            ("platform_capabilities", PlatformCapabilitiesBlock()),
            ("vertical_solution_cards", VerticalSolutionCardsBlock()),
            ("proof_strip", ProofStripBlock()),
            ("feature_grid", FeatureGridBlock()),
            ("ai_spotlight", AISpotlightBlock()),
            ("lead_capture", LeadCaptureBlock()),
            ("final_cta", FinalCTABlock()),
            ("html", blocks.RawHTMLBlock()),
        ],
        blank=True,
    )

    content_panels = Page.content_panels + [
        FieldPanel("hero_tagline"),
        FieldPanel("hero_title"),
        FieldPanel("hero_subtitle"),
        FieldPanel("hero_cta_text"),
        FieldPanel("hero_cta_url"),
        FieldPanel("hero_secondary_cta_text"),
        FieldPanel("hero_secondary_cta_url"),
        FieldPanel("hero_image"),
        FieldPanel("hero_image_alt"),
        FieldPanel("show_terminal_simulator"),
        FieldPanel("body", classname="full"),
    ]

    def serve(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            from django.http import HttpResponseRedirect
            from django.urls import reverse

            team = request.team
            if team:
                return HttpResponseRedirect(reverse("web_team:home", args=[team.slug]))
            else:
                from django.contrib import messages
                from django.utils.translation import gettext_lazy as _

                from apps.teams.helpers import get_open_invitations_for_user

                if (open_invitations := get_open_invitations_for_user(request.user)) and len(open_invitations) > 1:
                    invitation = open_invitations[0]
                    return HttpResponseRedirect(reverse("teams:accept_invitation", args=[invitation["id"]]))

                messages.info(
                    request,
                    _(
                        "Teams are enabled but you have no teams. Create a team below to access the rest "
                        "of the dashboard."
                    ),
                )
                return HttpResponseRedirect(reverse("teams:manage_teams"))
        return super().serve(request, *args, **kwargs)


class MarketingStandardPage(BaseContentPage):
    hero_tagline = models.CharField(max_length=255, blank=True)
    hero_title = models.CharField(max_length=255, blank=True)
    hero_subtitle = models.TextField(blank=True)
    hero_image = models.ForeignKey(
        "wagtailimages.Image", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    hero_image_alt = models.CharField(max_length=255, blank=True)

    body = StreamField(
        [
            ("hero", HeroBlock()),
            ("marketing_media", MarketingMediaBlock()),
            ("marketing_photo_grid", MarketingPhotoGridBlock()),
            ("marketing_journey", MarketingJourneyBlock()),
            ("marketing_solution", MarketingSolutionBlock()),
            ("trusted_by", TrustedByBlock()),
            ("metric_band", MetricBandBlock()),
            ("product_visual", ProductVisualBlock()),
            ("process", ProcessBlock()),
            ("platform_capabilities", PlatformCapabilitiesBlock()),
            ("vertical_solution_cards", VerticalSolutionCardsBlock()),
            ("proof_strip", ProofStripBlock()),
            ("feature_grid", FeatureGridBlock()),
            ("feature_section", FeatureSectionBlock()),
            ("pricing_tiers", PricingTiersBlock()),
            ("pricing_comparison", PricingComparisonBlock()),
            ("faq_accordion", FAQAccordionBlock()),
            ("solutions_section", SolutionsSectionBlock()),
            ("lead_capture", LeadCaptureBlock()),
            ("final_cta", FinalCTABlock()),
            ("paragraph", blocks.RichTextBlock()),
            ("html", blocks.RawHTMLBlock()),
        ],
        blank=True,
    )

    content_panels = Page.content_panels + [
        FieldPanel("hero_tagline"),
        FieldPanel("hero_title"),
        FieldPanel("hero_subtitle"),
        FieldPanel("hero_image"),
        FieldPanel("hero_image_alt"),
        FieldPanel("body", classname="full"),
    ]

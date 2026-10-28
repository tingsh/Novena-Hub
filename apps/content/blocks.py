from django.utils.html import format_html
from django.utils.safestring import mark_safe
from wagtail import blocks
from wagtail.images.blocks import ImageChooserBlock


class CaptionBlock(blocks.TextBlock):
    """
    A block for generating <figcaptions> that can also use html characters (so you can add, e.g. links).
    """

    def render_basic(self, value, context=None):
        if value:
            return format_html("<figcaption>{0}</figcaption>", mark_safe(value))
        else:
            return ""

    class Meta:
        icon = "info-circle"


class MarketingMediaBlock(blocks.StructBlock):
    eyebrow = blocks.CharBlock(required=False)
    title = blocks.CharBlock()
    body = blocks.TextBlock(required=False)
    image = ImageChooserBlock()
    image_alt = blocks.CharBlock(help_text="Describe the image for people using screen readers.")
    caption = blocks.CharBlock(required=False)
    kind = blocks.ChoiceBlock(choices=[("photo", "Field photography"), ("product", "Real product screen")])
    reverse = blocks.BooleanBlock(required=False)
    points = blocks.ListBlock(blocks.CharBlock(), required=False)
    link_text = blocks.CharBlock(required=False)
    link_url = blocks.CharBlock(required=False)

    class Meta:
        template = "content/blocks/marketing_media.html"
        icon = "image"
        label = "Marketing image and text"


class MarketingPhotoCardBlock(blocks.StructBlock):
    image = ImageChooserBlock()
    image_alt = blocks.CharBlock()
    label = blocks.CharBlock()
    title = blocks.CharBlock()
    link_url = blocks.CharBlock()


class MarketingPhotoGridBlock(blocks.StructBlock):
    eyebrow = blocks.CharBlock(required=False)
    title = blocks.CharBlock()
    intro = blocks.TextBlock(required=False)
    cards = blocks.ListBlock(MarketingPhotoCardBlock())

    class Meta:
        template = "content/blocks/marketing_photo_grid.html"
        icon = "grip"
        label = "Photo grid"


class MarketingJourneyStepBlock(blocks.StructBlock):
    number = blocks.CharBlock()
    title = blocks.CharBlock()
    detail = blocks.CharBlock()


class MarketingJourneyBlock(blocks.StructBlock):
    eyebrow = blocks.CharBlock(required=False)
    title = blocks.CharBlock()
    intro = blocks.TextBlock(required=False)
    steps = blocks.ListBlock(MarketingJourneyStepBlock())

    class Meta:
        template = "content/blocks/marketing_journey.html"
        icon = "list-ol"
        label = "Gateway to value journey"


class MarketingSolutionBlock(blocks.StructBlock):
    anchor_id = blocks.CharBlock(help_text="Anchor used by links, without the # character.")
    eyebrow = blocks.CharBlock()
    title = blocks.CharBlock()
    body = blocks.TextBlock()
    image = ImageChooserBlock()
    image_alt = blocks.CharBlock()
    screenshot = ImageChooserBlock(required=False)
    screenshot_alt = blocks.CharBlock(required=False)
    points = blocks.ListBlock(blocks.CharBlock())
    reverse = blocks.BooleanBlock(required=False)

    class Meta:
        template = "content/blocks/marketing_solution.html"
        icon = "image"
        label = "Photo-led solution"


class TrustedByBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Empowering Industry Leaders Across ASEAN")
    companies = blocks.ListBlock(blocks.CharBlock(label="Company Name"))

    class Meta:
        template = "content/blocks/trusted_by.html"
        icon = "group"
        label = "Trusted By / Partners"


class FeatureCardBlock(blocks.StructBlock):
    icon = blocks.CharBlock(required=True, default="fa-chart-line", help_text="FontAwesome class name")
    title = blocks.CharBlock(required=True)
    description = blocks.TextBlock(required=True)
    link_text = blocks.CharBlock(required=False)
    link_url = blocks.CharBlock(required=False)


class MetricItemBlock(blocks.StructBlock):
    value = blocks.CharBlock(required=True)
    label = blocks.CharBlock(required=True)
    detail = blocks.CharBlock(required=False)


class MetricBandBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Operational outcomes")
    title = blocks.CharBlock(required=False)
    metrics = blocks.ListBlock(MetricItemBlock())

    class Meta:
        template = "content/blocks/metric_band.html"
        icon = "pick"
        label = "Metric Band"


class ProductVisualBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Product view")
    title = blocks.CharBlock(required=True)
    subtitle = blocks.TextBlock(required=False)
    visual_type = blocks.ChoiceBlock(
        choices=[
            ("operations", "Operations Dashboard"),
            ("energy", "Energy Monitoring"),
            ("gateway", "Gateway Fleet"),
            ("ai", "AI Assistant"),
        ],
        default="operations",
    )
    bg_color = blocks.ChoiceBlock(
        choices=[
            ("white", "White"),
            ("light_gray", "Light Gray"),
            ("dark", "Dark"),
        ],
        default="white",
    )

    class Meta:
        template = "content/blocks/product_visual.html"
        icon = "image"
        label = "Product Visual"


class PlatformCapabilityBlock(blocks.StructBlock):
    anchor_id = blocks.CharBlock(required=False, help_text="Optional HTML anchor without #")
    icon = blocks.CharBlock(required=False, default="fa-chart-line")
    title = blocks.CharBlock(required=True)
    description = blocks.TextBlock(required=True)
    proof = blocks.CharBlock(required=False)


class PlatformCapabilitiesBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Platform")
    title = blocks.CharBlock(required=True)
    intro = blocks.TextBlock(required=False)
    capabilities = blocks.ListBlock(PlatformCapabilityBlock())

    class Meta:
        template = "content/blocks/platform_capabilities.html"
        icon = "cogs"
        label = "Platform Capabilities"


class ProcessStepBlock(blocks.StructBlock):
    step = blocks.CharBlock(required=True)
    title = blocks.CharBlock(required=True)
    description = blocks.TextBlock(required=True)


class ProcessBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="How it works")
    title = blocks.CharBlock(required=True)
    steps = blocks.ListBlock(ProcessStepBlock())

    class Meta:
        template = "content/blocks/process.html"
        icon = "list-ol"
        label = "Process"


class VerticalSolutionCardBlock(blocks.StructBlock):
    anchor_id = blocks.CharBlock(required=False, help_text="Optional HTML anchor without #")
    icon = blocks.CharBlock(required=False, default="fa-industry")
    vertical = blocks.CharBlock(required=True)
    title = blocks.CharBlock(required=True)
    pain = blocks.TextBlock(required=True)
    outcome = blocks.TextBlock(required=True)
    features = blocks.ListBlock(blocks.CharBlock(label="Feature"), required=False)
    cta_text = blocks.CharBlock(required=False, default="Explore solution")
    cta_url = blocks.CharBlock(required=False, default="/solutions/")


class VerticalSolutionCardsBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Solutions")
    title = blocks.CharBlock(required=True)
    intro = blocks.TextBlock(required=False)
    solutions = blocks.ListBlock(VerticalSolutionCardBlock())

    class Meta:
        template = "content/blocks/vertical_solution_cards.html"
        icon = "site"
        label = "Vertical Solution Cards"


class ProofPointBlock(blocks.StructBlock):
    label = blocks.CharBlock(required=False)
    title = blocks.CharBlock(required=True)
    body = blocks.TextBlock(required=True)
    metric = blocks.CharBlock(required=False)
    metric_label = blocks.CharBlock(required=False)


class ProofStripBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Proof")
    title = blocks.CharBlock(required=True)
    intro = blocks.TextBlock(required=False)
    proof_points = blocks.ListBlock(ProofPointBlock())

    class Meta:
        template = "content/blocks/proof_strip.html"
        icon = "success"
        label = "Proof / Case Study Strip"


class LeadCaptureBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Request a pilot")
    title = blocks.CharBlock(required=True, default="See Novena on your equipment")
    body = blocks.TextBlock(required=True)
    default_interest = blocks.CharBlock(required=False, default="Energy monitoring pilot")

    class Meta:
        template = "content/blocks/lead_capture.html"
        icon = "mail"
        label = "Lead Capture Form"


class FeatureGridBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Core Capabilities")
    title = blocks.CharBlock(required=True, default="Engineered for Extreme Operational Resilience.")
    features = blocks.ListBlock(FeatureCardBlock())

    class Meta:
        template = "content/blocks/feature_grid.html"
        icon = "grid"
        label = "Feature Grid"


class AISpotlightBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Built-in Intelligence")
    title = blocks.CharBlock(required=True, default="Stop searching for logs. Start asking questions.")
    body = blocks.TextBlock(required=True)
    prompt_text = blocks.CharBlock(required=True, default="Show yesterday's efficiency trends for Factory Floor A.")
    response_tagline = blocks.CharBlock(required=False, default="Novena AI")
    response_text = blocks.TextBlock(required=True)

    class Meta:
        template = "content/blocks/ai_spotlight.html"
        icon = "code"
        label = "AI Spotlight"


class FinalCTABlock(blocks.StructBlock):
    title = blocks.CharBlock(required=True, default="Ready to secure your operations?")
    subtitle = blocks.CharBlock(
        required=True, default="Connect your first edge gateway in 24 hours. Activate your pilot program today."
    )
    primary_cta_text = blocks.CharBlock(required=True, default="Activate Pilot Program")
    primary_cta_url = blocks.CharBlock(required=True, default="/accounts/signup/")
    secondary_cta_text = blocks.CharBlock(required=False, default="See Plans & Pricing")
    secondary_cta_url = blocks.CharBlock(required=False, default="/pricing/")

    class Meta:
        template = "content/blocks/final_cta.html"
        icon = "plus"
        label = "Final Call to Action"


class FeatureSectionBlock(blocks.StructBlock):
    title = blocks.CharBlock(required=True)
    description = blocks.TextBlock(required=True)
    icon = blocks.CharBlock(required=False, default="fa-chart-line")
    badge = blocks.CharBlock(required=False)
    check_items = blocks.ListBlock(blocks.CharBlock(label="Bullet Point"), required=False)
    mockup_type = blocks.ChoiceBlock(
        choices=[
            ("dashboard", "Dashboard Mockup"),
            ("ai_chat", "AI Chat Mockup"),
            ("remote_control", "Remote Control Mockup"),
            ("multi_wan", "Multi-WAN Active Watchdog"),
        ],
        default="dashboard",
    )
    mockup_title = blocks.CharBlock(required=False, default="Mockup")
    align_image = blocks.ChoiceBlock(
        choices=[
            ("left", "Image Left"),
            ("right", "Image Right"),
        ],
        default="right",
    )
    bg_color = blocks.ChoiceBlock(
        choices=[
            ("white", "White"),
            ("light_gray", "Light Gray"),
            ("dark_indigo", "Dark Indigo"),
        ],
        default="white",
    )

    class Meta:
        template = "content/blocks/feature_section.html"
        icon = "doc-full-blank"
        label = "Feature Section (Product Layout)"


class PricingTierBlock(blocks.StructBlock):
    name = blocks.CharBlock(required=True)
    badge = blocks.CharBlock(required=False)
    price = blocks.CharBlock(required=True)
    period = blocks.CharBlock(required=False, default="/mo")
    features = blocks.ListBlock(blocks.CharBlock(label="Feature Item"))
    cta_text = blocks.CharBlock(required=True, default="Get Started")
    cta_url = blocks.CharBlock(required=True, default="/accounts/signup/")
    is_popular = blocks.BooleanBlock(required=False, default=False)


class PricingTiersBlock(blocks.StructBlock):
    title = blocks.CharBlock(required=False, default="Simple, fair pricing.")
    subtitle = blocks.CharBlock(required=False, default="No hidden device fees. No complex throughput math.")
    tiers = blocks.ListBlock(PricingTierBlock())

    class Meta:
        template = "content/blocks/pricing_tiers.html"
        icon = "table"
        label = "Pricing Tiers Grid"


class ComparisonFeatureRow(blocks.StructBlock):
    name = blocks.CharBlock(required=True)
    starter = blocks.CharBlock(required=True)
    professional = blocks.CharBlock(required=True)
    business = blocks.CharBlock(required=True)


class PricingComparisonBlock(blocks.StructBlock):
    title = blocks.CharBlock(required=False, default="Compare Features")
    features = blocks.ListBlock(ComparisonFeatureRow())

    class Meta:
        template = "content/blocks/pricing_comparison.html"
        icon = "list-ul"
        label = "Pricing Comparison Table"


class FAQAccordionBlock(blocks.StructBlock):
    title = blocks.CharBlock(required=False, default="Frequently Asked Questions")
    faqs = blocks.ListBlock(
        blocks.StructBlock(
            [
                ("question", blocks.CharBlock(required=True)),
                ("answer", blocks.TextBlock(required=True)),
            ]
        )
    )

    class Meta:
        template = "content/blocks/faq_accordion.html"
        icon = "help"
        label = "FAQ Accordion"


class SolutionsSectionBlock(blocks.StructBlock):
    badge = blocks.CharBlock(required=True)
    title = blocks.CharBlock(required=True)
    pain_point_text = blocks.TextBlock(required=True)
    solution_text = blocks.TextBlock(required=True)
    illustration_type = blocks.ChoiceBlock(
        choices=[
            ("grid", "Grids/Stats"),
            ("snowflake", "Snowflake Icon"),
            ("gear", "Rotating Gear"),
        ],
        default="grid",
    )
    align_image = blocks.ChoiceBlock(
        choices=[
            ("left", "Image Left"),
            ("right", "Image Right"),
        ],
        default="right",
    )
    bg_color = blocks.ChoiceBlock(
        choices=[
            ("white", "White"),
            ("light_gray", "Light Gray"),
        ],
        default="white",
    )

    class Meta:
        template = "content/blocks/solutions_section.html"
        icon = "tasks"
        label = "Solution Section (Verticals)"


class HeroBlock(blocks.StructBlock):
    tagline = blocks.CharBlock(required=False, default="Industrial IoT for SME operations")
    title = blocks.CharBlock(required=True, default="Connect equipment. See operations clearly. Act faster.")
    subtitle = blocks.CharBlock(required=True)
    primary_cta_text = blocks.CharBlock(required=True, default="Sign up")
    primary_cta_url = blocks.CharBlock(required=True, default="/accounts/signup/")
    secondary_cta_text = blocks.CharBlock(required=False, default="Explore Platform")
    secondary_cta_url = blocks.CharBlock(required=False, default="#features")
    bg_style = blocks.ChoiceBlock(
        choices=[
            ("light", "Light Background"),
            ("dark", "Dark Background"),
        ],
        default="light",
    )
    show_terminal_simulator = blocks.BooleanBlock(required=False, default=True)

    class Meta:
        template = "content/blocks/hero.html"
        icon = "home"
        label = "Hero Section"

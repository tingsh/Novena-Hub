"""Import reviewed marketing media and seed the public site once.

Use --refresh only for the initial redesign rollout. Later editor changes are left alone.
"""

# Long literals below are editorial copy retained as readable sentences.
# ruff: noqa: E501

from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand
from wagtail.images import get_image_model
from wagtail.models import Site

from apps.content.models import BlogIndexPage, BlogPage, BlogPageGalleryImage, MarketingStandardPage, NovenaHomePage

MEDIA = {
    "factory": "Factory operator working with industrial equipment",
    "energy": "Technician inspecting an electrical installation",
    "cold-chain": "Worker inside a temperature-controlled storage room",
    "manufacturing": "Industrial operator at a machinery control panel",
    "facilities": "Modern commercial building exterior",
}
SCREENS = {
    "operations": "Novena Hub operations overview with demonstration data",
    "energy-screen": "Novena Hub energy monitoring with demonstration data",
    "gateway-claim": "Novena Hub Gateway claim screen with demonstration data",
    "template-setup": "Novena Hub device template setup with demonstration data",
    "alerts-screen": "Novena Hub alerts with demonstration data",
}


def block(name, value):
    return {"type": name, "value": value}


def media(image, eyebrow, title, body, *, kind="photo", reverse=False, points=None, link=None, caption=None):
    return block(
        "marketing_media",
        {
            "eyebrow": eyebrow,
            "title": title,
            "body": body,
            "image": image.id,
            "image_alt": image.title,
            "caption": caption or ("Novena Hub · demonstration data" if kind == "product" else ""),
            "kind": kind,
            "reverse": reverse,
            "points": points or [],
            "link_text": link[0] if link else "",
            "link_url": link[1] if link else "",
        },
    )


def journey():
    return block(
        "marketing_journey",
        {
            "eyebrow": "From edge to evidence",
            "title": "A clear path from equipment to a trusted reading.",
            "intro": "Start with one useful signal. Carry its identity and context through each setup step.",
            "steps": [
                {
                    "number": "01",
                    "title": "Connect",
                    "detail": "Place a Gateway near the equipment and confirm field connectivity.",
                },
                {
                    "number": "02",
                    "title": "Claim",
                    "detail": "Assign the Gateway to the right team and site in Novena Hub.",
                },
                {
                    "number": "03",
                    "title": "Apply a template",
                    "detail": "Map device signals and units with a reusable template.",
                },
                {
                    "number": "04",
                    "title": "Trust the signal",
                    "detail": "Check freshness, thresholds, and a readable trend before operators act.",
                },
            ],
        },
    )


def photo_grid(img):
    cards = [
        ("energy", "01 / Energy", "Understand every peak", "/solutions/#energy"),
        ("cold-chain", "02 / Cold chain", "Protect what must stay cold", "/solutions/#cold-chain"),
        ("manufacturing", "03 / Manufacturing", "See machine behavior", "/solutions/#manufacturing"),
        ("facilities", "04 / Facilities", "Operate across sites", "/solutions/#facilities"),
    ]
    return block(
        "marketing_photo_grid",
        {
            "eyebrow": "Where Novena works",
            "title": "One platform. Four practical starting points.",
            "intro": "Choose a use case that makes the first site measurable.",
            "cards": [
                {"image": img[key].id, "image_alt": img[key].title, "label": label, "title": title, "link_url": url}
                for key, label, title, url in cards
            ],
        },
    )


def solution(img, key, eyebrow, title, body, points, reverse=False, screenshot=None):
    value = {
        "anchor_id": key,
        "eyebrow": eyebrow,
        "title": title,
        "body": body,
        "image": img[key].id,
        "image_alt": img[key].title,
        "points": points,
        "reverse": reverse,
    }
    if screenshot:
        value["screenshot"] = screenshot.id
        value["screenshot_alt"] = screenshot.title
    return block("marketing_solution", value)


def cta(title, subtitle, secondary="Sign up", secondary_url="/accounts/signup/"):
    return block(
        "final_cta",
        {
            "title": title,
            "subtitle": subtitle,
            "primary_cta_text": "Request demo",
            "primary_cta_url": "/about/#contact",
            "secondary_cta_text": secondary,
            "secondary_cta_url": secondary_url,
        },
    )


def lead(title, body, interest="General demo request"):
    return block(
        "lead_capture", {"tagline": "Start a conversation", "title": title, "body": body, "default_interest": interest}
    )


class Command(BaseCommand):
    help = "Import marketing images and create resources; --refresh replaces existing marketing page copy once."

    def add_arguments(self, parser):
        parser.add_argument(
            "--refresh", action="store_true", help="Replace existing public marketing page copy for initial rollout"
        )
        parser.add_argument(
            "--configure-domain", action="store_true", help="Set the default Wagtail site to MARKETING_SITE_DOMAIN"
        )

    def handle(self, *args, **options):
        assets = Path(settings.BASE_DIR) / "static" / "marketing"
        Image = get_image_model()
        images = {}
        for key, title in {**MEDIA, **SCREENS}.items():
            ext = "webp" if key in MEDIA else "png"
            path = assets / f"{key}.{ext}"
            if not path.exists():
                if key in SCREENS:
                    self.stdout.write(self.style.WARNING(f"Missing product screen: {path.name}"))
                    continue
                raise FileNotFoundError(path)
            image = Image.objects.filter(title=title).first()
            if not image:
                with path.open("rb") as source:
                    image = Image(title=title)
                    image.file.save(path.name, File(source), save=True)
            elif options["refresh"] and key in SCREENS:
                with path.open("rb") as source:
                    image.file.save(path.name, File(source), save=True)
                image.renditions.all().delete()
            images[key] = image
        home = NovenaHomePage.objects.first()
        if not home:
            raise RuntimeError("Novena home page is missing")
        if options["refresh"]:
            self.refresh_pages(home, images)
        self.seed_resources(home, images)
        if options["configure_domain"]:
            site = Site.objects.filter(is_default_site=True).first()
            if not site:
                raise RuntimeError("Default Wagtail site is missing")
            site.hostname = settings.MARKETING_SITE_DOMAIN
            site.port = 443
            site.site_name = "Novena Platform"
            site.save(update_fields=["hostname", "port", "site_name"])
        self.stdout.write(self.style.SUCCESS("Marketing assets and resources ready"))

    def refresh_pages(self, home, img):
        home.hero_tagline = "From equipment to action"
        home.hero_title = "Bring physical operations into view."
        home.hero_subtitle = "Connect field equipment to Novena Hub. Claim a Gateway, apply a device template, and give your team a clear view of what needs attention."
        home.hero_cta_text, home.hero_cta_url = "Request demo", "/about/#contact"
        home.hero_secondary_cta_text, home.hero_secondary_cta_url = "Sign up", "/accounts/signup/"
        home.hero_image = img.get("operations") or img["factory"]
        home.hero_image_alt = home.hero_image.title
        home.seo_title = "Bring physical operations into view"
        home.search_description = "Novena connects field equipment to clear monitoring and alerts through Gateway claim, reusable templates, and Novena Hub."
        home.body = [
            media(
                img["factory"],
                "Built for real operations",
                "See the site behind the signal.",
                "Operators need context from the field, not another wall of data. Novena starts with the equipment and people who keep a site running.",
                reverse=True,
                points=[
                    "Start with one operational problem",
                    "Connect the equipment that matters",
                    "Review the first readings with the team",
                ],
                link=("See solutions", "/solutions/"),
            ),
            journey(),
            media(
                img.get("gateway-claim") or img["energy"],
                "Guided onboarding",
                "Claim the Gateway. Keep the setup legible.",
                "Assign a Gateway to a site, then use device templates to give incoming readings a name, unit, and purpose.",
                kind="product" if img.get("gateway-claim") else "photo",
                reverse=True,
                points=[
                    "Gateway ownership and site assignment",
                    "Reusable device templates",
                    "Readable signal freshness",
                ],
                link=("Explore the platform", "/product/"),
            ),
            photo_grid(img),
            media(
                img.get("alerts-screen") or img["manufacturing"],
                "From monitoring to action",
                "An alert is useful when someone knows what to do next.",
                "Bring exceptions into the operator's daily view and connect them to the equipment and site behind them.",
                kind="product" if img.get("alerts-screen") else "photo",
                reverse=True,
                link=("Discuss a pilot", "/about/#contact"),
            ),
            cta(
                "Prove value on one site.",
                "A supervised pilot can begin with one Gateway, a focused equipment group, and a signal your team can verify.",
            ),
        ]
        home.save_revision().publish()
        pages = {
            p.slug: p
            for p in MarketingStandardPage.objects.filter(slug__in=["product", "solutions", "pricing", "about"])
        }
        if len(pages) != 4:
            raise RuntimeError("Expected four existing marketing pages")
        product = pages["product"]
        product.hero_tagline = "The Novena platform"
        product.hero_title = "From field signals to confident decisions."
        product.hero_subtitle = "A managed Gateway brings equipment readings into Novena Hub, where teams can check setup, see trends, and act on exceptions."
        product.hero_image = img.get("operations") or img["factory"]
        product.hero_image_alt = product.hero_image.title
        product.body = [
            journey(),
            media(
                img.get("gateway-claim") or img["energy"],
                "01 / Claim",
                "Put the Gateway in the right hands.",
                "Claiming ties a Gateway to a team and site before any readings are used operationally.",
                kind="product" if img.get("gateway-claim") else "photo",
                points=["Assign ownership", "Confirm site context", "Check connection status"],
            ),
            media(
                img.get("template-setup") or img["factory"],
                "02 / Configure",
                "Make every signal understandable.",
                "A device template maps measurements, units, and thresholds so the next deployment starts from a known setup.",
                kind="product" if img.get("template-setup") else "photo",
                reverse=True,
                points=["Reusable mapping", "Device-specific context", "Clear units and thresholds"],
            ),
            media(
                img.get("energy-screen") or img["energy"],
                "03 / Monitor",
                "See changes before they become surprises.",
                "Use Hub views to inspect site status, energy patterns, and readings. Check timestamps and data freshness alongside the values.",
                kind="product" if img.get("energy-screen") else "photo",
                points=["Site and device views", "Time-series trends", "Reading freshness"],
            ),
            media(
                img.get("alerts-screen") or img["manufacturing"],
                "04 / Respond",
                "Turn exceptions into a next step.",
                "Review alert context and route work to the operator responsible for that equipment.",
                kind="product" if img.get("alerts-screen") else "photo",
                reverse=True,
                points=["Threshold alerts", "Equipment context", "Maintenance follow-up"],
            ),
            cta(
                "Walk through the product on your use case.",
                "Bring a meter list, equipment map, or one operational question. We'll show the relevant Hub workflow.",
            ),
        ]
        solutions = pages["solutions"]
        solutions.hero_tagline = "Solutions"
        solutions.hero_title = "Start with the operating problem that matters most."
        solutions.hero_subtitle = "The same edge-to-Hub foundation can support energy, cold chain, manufacturing, and facilities. Begin with one measurable use case."
        solutions.hero_image = img["energy"]
        solutions.hero_image_alt = img["energy"].title
        solutions.body = [
            solution(
                img,
                "energy",
                "Energy",
                "Understand load before the bill arrives.",
                "Disconnected meters can hide demand spikes and unusual consumption. Bring readings into one view so teams can review where energy is going.",
                ["Compare load over time", "Set peak and abnormal-use alerts", "Review site and equipment context"],
                screenshot=img.get("energy-screen"),
            ),
            solution(
                img,
                "cold-chain",
                "Cold chain",
                "Catch temperature drift while there is time to respond.",
                "Manual checks leave gaps between readings. Monitor a cold room's signals and surface exceptions to the people on duty.",
                ["Track temperature trends", "See door and equipment context", "Escalate threshold breaches"],
                reverse=True,
            ),
            solution(
                img,
                "manufacturing",
                "Manufacturing",
                "Give machine signals an operating context.",
                "Machine faults and run hours often stay on the floor. Make selected readings visible to supervisors and maintenance teams.",
                ["Monitor run hours and faults", "Trace a change to a device", "Connect alerts to follow-up work"],
            ),
            solution(
                img,
                "facilities",
                "Facilities",
                "See the sites behind the service tickets.",
                "Facilities teams need a shared picture of equipment health across buildings. Start with the assets that generate repeat calls.",
                ["Compare site status", "Watch HVAC and utility signals", "Prioritize the next visit"],
                reverse=True,
            ),
            lead(
                "Choose a focused first site.",
                "Tell us the asset or site you want to understand. We'll scope a supervised pilot around a signal your team can verify.",
                "Solution fit discussion",
            ),
        ]
        pricing = pages["pricing"]
        pricing.hero_tagline = "Indicative pricing"
        pricing.hero_title = "Start small. Expand with evidence."
        pricing.hero_subtitle = "Indicative monthly platform pricing for planning. Final scope and commercial terms are agreed for each deployment."
        pricing.hero_image = None
        pricing.body = [
            block(
                "pricing_tiers",
                {
                    "title": "Choose the scope that fits your site.",
                    "subtitle": "Platform subscription estimates in Singapore dollars per month. Hardware, installation, and connectivity are quoted separately.",
                    "tiers": [
                        {
                            "name": "Starter",
                            "badge": "First site",
                            "price": "S$99",
                            "period": "/month, indicative",
                            "features": [
                                "Connect 1 Gateway",
                                "Up to 5 monitored devices",
                                "7 days of visible telemetry history",
                                "Email threshold alerts",
                            ],
                            "cta_text": "Request demo",
                            "cta_url": "/about/#contact",
                            "is_popular": False,
                        },
                        {
                            "name": "Business",
                            "badge": "Growing operation",
                            "price": "S$299",
                            "period": "/month, indicative",
                            "features": [
                                "Connect 3 Gateways",
                                "Up to 20 monitored devices",
                                "30 days of visible telemetry history",
                                "Expanded monitoring workflows",
                            ],
                            "cta_text": "Request demo",
                            "cta_url": "/about/#contact",
                            "is_popular": True,
                        },
                        {
                            "name": "Enterprise",
                            "badge": "Custom scope",
                            "price": "Custom",
                            "period": "",
                            "features": [
                                "Gateway limits scoped with your team",
                                "Up to 100 monitored devices in standard metadata",
                                "90 days of visible telemetry history",
                                "Deployment and support scope agreed separately",
                            ],
                            "cta_text": "Contact us",
                            "cta_url": "/about/#contact",
                            "is_popular": False,
                        },
                    ],
                },
            ),
            block(
                "pricing_comparison",
                {
                    "title": "At a glance",
                    "features": [
                        {"name": "Gateway connections", "starter": "1", "professional": "3", "business": "Custom"},
                        {"name": "Monitored devices", "starter": "5", "professional": "20", "business": "Up to 100"},
                        {
                            "name": "Visible telemetry history",
                            "starter": "7 days",
                            "professional": "30 days",
                            "business": "90 days",
                        },
                    ],
                },
            ),
            media(
                img["energy"],
                "What the platform price covers",
                "A clear boundary around the subscription.",
                "The monthly platform estimate covers software access for the stated limits. Gateway hardware, field installation, SIM or other connectivity, and any bespoke integration are scoped separately.",
                reverse=True,
                points=[
                    "No implied hardware inclusion",
                    "Pilot scope agreed before installation",
                    "Subscription limits match current plan metadata",
                ],
            ),
            cta(
                "Price the pilot around your equipment.",
                "Share the site, signal list, and installation needs. We'll return a practical scope.",
            ),
        ]
        about = pages["about"]
        about.hero_tagline = "About Novena"
        about.hero_title = "Physical operations should be easier to understand."
        about.hero_subtitle = "Novena is building a practical edge-to-cloud platform for lean operations teams in Singapore and the region."
        about.hero_image = img["factory"]
        about.hero_image_alt = img["factory"].title
        about.body = [
            media(
                img["manufacturing"],
                "Our mission",
                "A shared operating picture for people closest to the work.",
                "Our focus is the path from connected equipment to a trustworthy reading and a clear next action. We design for technicians, supervisors, and managers who need to work from the same facts.",
                reverse=True,
                points=[
                    "Field-first onboarding",
                    "Readable operational context",
                    "A measured path from pilot to rollout",
                ],
            ),
            media(
                img["facilities"],
                "Where we are",
                "Building with supervised pilot teams.",
                "Novena is at an early deployment stage. We work with pilot partners to validate data quality, site workflows, and measurable outcomes before broader rollout.",
                points=[
                    "Focused first-site scope",
                    "Pilot findings reviewed with operators",
                    "A clear decision before wider rollout",
                ],
            ),
            lead(
                "Show us the site you want to understand.",
                "Tell us what equipment, signals, or operational problem you have in mind. Our team will respond with a relevant walkthrough.",
            ),
        ]
        for page in pages.values():
            page.seo_title = page.hero_title
            page.search_description = page.hero_subtitle[:155]
            page.save_revision().publish()

    def seed_resources(self, home, img):
        index = BlogIndexPage.objects.filter(slug="resources", path__startswith=home.path).first()
        if not index:
            index = BlogIndexPage(
                title="Resources", slug="resources", intro="Practical guides for the first connected site."
            )
            home.add_child(instance=index)
            index.save_revision().publish()
        guides = [
            (
                "scope-an-industrial-iot-pilot",
                "How to scope a useful first pilot",
                "A short checklist for choosing one site, one signal, and a measurable review.",
                "factory",
                [
                    (
                        "Choose a decision, not a dashboard",
                        "Start with a recurring operating question: which load created the peak, when did a room drift, or why did a machine stop? Name the person who needs the answer and the decision they will make.",
                    ),
                    (
                        "Keep the first site narrow",
                        "Choose one site and a small equipment group. Record the meters, PLCs, or sensors already available, the expected units, and any access or installation constraints.",
                    ),
                    (
                        "Define a trustworthy reading",
                        "Agree how the team will confirm signal identity, units, timestamp freshness, and a normal operating range. The first milestone is a reading that a technician trusts.",
                    ),
                    (
                        "Review outcomes together",
                        "At the pilot review, look at data availability, alert usefulness, operator response, and the effort to repeat the setup. Expand only where the evidence supports it.",
                    ),
                ],
            ),
            (
                "gateway-claim-to-first-signal",
                "From Gateway claim to the first trusted signal",
                "The steps that connect field equipment to a reading people can use.",
                "energy",
                [
                    (
                        "Connect and identify",
                        "Install the Gateway in the agreed location and record its identity. Confirm the field network and data source before attaching it to a production workflow.",
                    ),
                    (
                        "Claim to the right team and site",
                        "Use Novena Hub to assign the Gateway to the operating team and site. This gives incoming readings a clear owner and physical context.",
                    ),
                    (
                        "Apply a device template",
                        "Map source addresses or tags to named measurements with units. Reuse a reviewed template where the device model and configuration match.",
                    ),
                    (
                        "Verify before alerting",
                        "Compare the Hub reading with the field instrument, check the timestamp, and set a sensible threshold. Only then invite operators to act on an alert.",
                    ),
                ],
            ),
            (
                "first-site-energy-monitoring",
                "A first-site energy monitoring playbook",
                "How to make a meter reading useful for daily operations and a pilot review.",
                "energy",
                [
                    (
                        "Pick the useful meter",
                        "Start with a main incomer or a critical submeter that answers a real question about load. Record meter type, interval, units, and any existing baseline.",
                    ),
                    (
                        "Check the shape of the day",
                        "Plot the first days of readings and confirm whether shift changes, start-up periods, and overnight load look plausible to the site team.",
                    ),
                    (
                        "Set a focused alert",
                        "Choose one condition such as an unusual peak or unexpected overnight draw. Review alert timing and false positives with the person who will receive it.",
                    ),
                    (
                        "Document the pilot result",
                        "Summarize data coverage, observed patterns, and actions taken. Treat estimated savings as assumptions until the site can verify them against a baseline.",
                    ),
                ],
            ),
        ]
        for slug, title, intro, image_key, sections in guides:
            if BlogPage.objects.filter(slug=slug, path__startswith=index.path).exists():
                continue
            body = [block("paragraph", f"<h2>{heading}</h2><p>{text}</p>") for heading, text in sections]
            guide = BlogPage(
                title=title,
                slug=slug,
                date=date.today(),
                intro=intro,
                body=body,
                search_description=intro,
                seo_title=title,
            )
            index.add_child(instance=guide)
            BlogPageGalleryImage.objects.create(page=guide, image=img[image_key], caption=img[image_key].title)
            guide.save_revision().publish()

import json
from django.db import migrations

def create_wagtail_pages(apps, schema_editor):
    from wagtail.models import Page
    from django.contrib.contenttypes.models import ContentType
    from django.db import connection
    MarketingStandardPage = apps.get_model('content', 'MarketingStandardPage')

    # 1. Convert homepage ID=2 to NovenaHomePage using raw SQL
    try:
        nodeflow_homepage_ct, _ = ContentType.objects.get_or_create(app_label='content', model='nodeflowhomepage')

        with connection.cursor() as cursor:
            # Delete any existing entries in content_homepage and content_nodeflowhomepage for ID=2
            cursor.execute("DELETE FROM content_homepage WHERE page_ptr_id = 2;")
            cursor.execute("DELETE FROM content_nodeflowhomepage WHERE page_ptr_id = 2;")
            
            # Insert the new NovenaHomePage record manually
            cursor.execute("""
                INSERT INTO content_nodeflowhomepage (
                    page_ptr_id, hero_tagline, hero_title, hero_subtitle, 
                    hero_cta_text, hero_cta_url, hero_secondary_cta_text, 
                    hero_secondary_cta_url, show_terminal_simulator, body
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
            """, [
                2,
                "Industrial Intelligence",
                "Your Factory, Perfectly Synchronized.",
                "Connect industrial PLCs, meters, and sensors to a secure unified cloud plane. Harness AI-guided template registry, local edge network failover, and zero-downtime upgrades in 24 hours.",
                "Activate Cloud Access",
                "/accounts/signup/",
                "Explore Platform",
                "#features",
                True,
                json.dumps([
                    {"type": "trusted_by", "value": {
                        "tagline": "Empowering Industry Leaders ASEAN-wide",
                        "companies": ["SOLAR-AXIS", "MECH-IND", "FLOW-CORP", "NANO-GRID", "ECO-THERM"]
                    }},
                    {"type": "feature_grid", "value": {
                        "tagline": "Core Capabilities",
                        "title": "Engineered for Extreme Operational Resilience.",
                        "features": [
                            {
                                "icon": "fa-chart-line",
                                "title": "Instant Auto-Dashboards",
                                "description": "Plug your equipment in, and watch Novena handle the configuration. Our schema parser matches registered templates (Omron, Schneider, Delta, Siemens) and auto-creates live WebSocket-streamed dashboards instantly.",
                                "link_text": "Learn template matching",
                                "link_url": "/product/#dashboards"
                            },
                            {
                                "icon": "fa-shield-halved",
                                "title": "Multi-WAN Edge Watchdog",
                                "description": "Zero data loss, even during connection dropouts. Our 3-tier Network Watchdog monitors links and dynamically metric-swaps default routes between Ethernet, Wi-Fi, and 4G/LTE without disconnecting local Modbus TCP polling loops.",
                                "link_text": "Explore WAN watchdog",
                                "link_url": "/product/#edge-resilience"
                            },
                            {
                                "icon": "fa-rotate",
                                "title": "Zero-Downtime OTA",
                                "description": "Deploy edge configuration and gateway updates without stopping telemetry loops. Our atomic system updates execute software transitions side-by-side (blue/green swaps) and rollback instantly on deployment faults.",
                                "link_text": "See upgrade blueprints",
                                "link_url": "/product/#edge-resilience"
                            }
                        ]
                    }},
                    {"type": "ai_spotlight", "value": {
                        "tagline": "Built-in Intelligence",
                        "title": "Stop searching for logs. <br/><span class=\"text-white/40\">Start asking questions.</span>",
                        "body": "Traditional IoT platforms force you to query raw Timescale databases or export thousands of rows into Excel. With Novena AI, query machinery trends, energy consumption, and alert history in plain English.",
                        "prompt_text": "Show yesterday's efficiency trends for Factory Floor A.",
                        "response_tagline": "Novena AI",
                        "response_text": "Analyzing Timescale data: Factory Floor A peaked at 88.4% capacity at 14:15. Total energy saved vs grid baseline: 142 kWh."
                    }},
                    {"type": "final_cta", "value": {
                        "title": "Ready to secure your operations?",
                        "subtitle": "Connect your first edge gateway in 24 hours. Activate your pilot program today.",
                        "primary_cta_text": "Activate Pilot Program",
                        "primary_cta_url": "/accounts/signup/",
                        "secondary_cta_text": "See Plans & Pricing",
                        "secondary_cta_url": "/pricing/"
                    }}
                ])
            ])
            
            # Defer updating the page content type until after child pages are added.

    except Exception as e:
        print(f"Error converting home page in migration: {e}")

    # Use the real Wagtail Page class for tree operations such as add_child().
    homepage = Page.objects.get(id=2)

    # 2. Add subpages under the converted home page
    subpages_data = [
        {
            "title": "Product Capabilities",
            "slug": "product",
            "hero_tagline": "Everything you need",
            "hero_title": "Everything you need,<br/><span class=\"text-indigo-600\">nothing you don't.</span>",
            "hero_subtitle": "Novena replaces complex, fragmented IoT stacks with a single, beautiful platform built specifically for industrial energy intelligence.",
            "body": [
                ("feature_section", {
                    "title": "Real-Time Dashboards & Vision",
                    "description": "See your facility's energy profile in high definition. Our Command Center uses TimescaleDB for sub-second telemetry ingestion and pixel-perfect data visualization.",
                    "icon": "fa-chart-line",
                    "badge": "Monitoring",
                    "check_items": [
                        "Custom KPI strips for power, energy, and efficiency metrics.",
                        "60-second HTMX auto-refresh loops.",
                        "Historical trend analysis with multi-device comparison."
                    ],
                    "mockup_type": "dashboard",
                    "align_image": "right",
                    "bg_color": "white"
                }),
                ("feature_section", {
                    "title": "Chat With Your Fleet",
                    "description": "Ask natural language questions about your facility's performance. Our AI Assistant understands your telemetry keys and can generate instant reports.",
                    "icon": "fa-robot",
                    "badge": "AI",
                    "check_items": [],
                    "mockup_type": "ai_chat",
                    "align_image": "left",
                    "bg_color": "light_gray"
                }),
                ("feature_section", {
                    "title": "Write-Back & RPC Control",
                    "description": "Don't just watch—take action. Securely toggle loads, adjust setpoints, and send commands back to your PLCs (Siemens S7, Modbus) directly from the cloud.",
                    "icon": "fa-toggle-on",
                    "badge": "Remote Control",
                    "check_items": [
                        "RBAC protection for critical control actions.",
                        "Full audit trail of who sent what command and when.",
                        "ThingsBoard Gateway compliant MQTT protocol."
                    ],
                    "mockup_type": "remote_control",
                    "align_image": "right",
                    "bg_color": "white"
                }),
                ("feature_grid", {
                    "tagline": "Cloud Logic",
                    "title": "Automate your facility.",
                    "features": [
                        {
                            "icon": "fa-code-fork",
                            "title": "IFTTT Conditionals",
                            "description": "Build complex logic like \"If Solar Power < 5kW for 10 mins AND Consumption > 50kW, then shed Load A.\""
                        },
                        {
                            "icon": "fa-clock",
                            "title": "Duration Tracking",
                            "description": "Prevent false triggers with Redis-backed state tracking. Ensure conditions stay active for X minutes before firing."
                        },
                        {
                            "icon": "fa-paper-plane",
                            "title": "Multi-Action Output",
                            "description": "Simultaneously send RPC commands, trigger webhooks to ERPs, and notify the team via WhatsApp or Email."
                        }
                    ]
                }),
                ("feature_section", {
                    "title": "Hardened Edge & Network Resilience",
                    "description": "Our industrial gateway runs directly on Raspberry Pi CM4 architectures, engineered specifically to prevent telemetry data loss on unstable factory floors.",
                    "icon": "fa-shield-halved",
                    "badge": "Edge Hardware & Resilience",
                    "check_items": [
                        "3-Tier Multi-WAN Watchdog: Automatically switches route metrics between Ethernet (primary), Wi-Fi (backup), and 4G/LTE (fallback).",
                        "Zero-Downtime Blue/Green OTA: Upgrade edge logic configurations side-by-side without interrupting Modbus TCP loops.",
                        "Offline Telemetry Cache: Spool data to local storage during WAN disconnects and sync back automatically."
                    ],
                    "mockup_type": "multi_wan",
                    "align_image": "right",
                    "bg_color": "light_gray"
                }),
                ("final_cta", {
                    "title": "Ready to see it in action?",
                    "subtitle": "Connect your first edge gateway in 24 hours. Activate your pilot program today.",
                    "primary_cta_text": "Start Free Trial",
                    "primary_cta_url": "/accounts/signup/",
                    "secondary_cta_text": "",
                    "secondary_cta_url": ""
                })
            ]
        },
        {
            "title": "Vertical Solutions",
            "slug": "solutions",
            "hero_tagline": "Vertical Solutions",
            "hero_title": "Tailored for the<br/><span class=\"text-transparent bg-clip-text bg-gradient-to-r from-indigo-400 to-sky-400\">factory floor.</span>",
            "hero_subtitle": "Generic IoT platforms fail because they don't understand industrial protocols or SME workflows. Novena was built from the ground up for industrial reality.",
            "body": [
                ("solutions_section", {
                    "badge": "Sustainability",
                    "title": "Energy & Grid Intelligence",
                    "pain_point_text": "Our electricity bills are spikey, but we have no idea which machine is causing the peak surges or how to claim EEG grants.",
                    "solution_text": "Automated submetering with peak shaving alerts. Gain full visibility into solar generation offset vs. consumption in real-time.",
                    "illustration_type": "grid",
                    "align_image": "right",
                    "bg_color": "white"
                }),
                ("solutions_section", {
                    "badge": "Food & Pharma",
                    "title": "Cold Chain Compliance",
                    "pain_point_text": "If a freezer door is left open overnight, we lose $20,000 of stock. SFA requires constant logging which our staff forget to do.",
                    "solution_text": "Digital temperature logging with multi-channel alerts (WhatsApp/SMS). Automated daily PDF reports for SFA auditors.",
                    "illustration_type": "snowflake",
                    "align_image": "left",
                    "bg_color": "light_gray"
                }),
                ("solutions_section", {
                    "badge": "Maintenance",
                    "title": "Factory Floor Automation",
                    "pain_point_text": "Our technician only fixes things when they break. We have zero data on run-hours across our 50 motors.",
                    "solution_text": "VFD and PLC monitoring integrated with a full CMMS (Maintenance System). Auto-trigger PM tickets based on actual motor hours.",
                    "illustration_type": "gear",
                    "align_image": "right",
                    "bg_color": "white"
                }),
                ("final_cta", {
                    "title": "Ready for a smart factory?",
                    "subtitle": "Activate your pilot program today.",
                    "primary_cta_text": "Start Pilot Program",
                    "primary_cta_url": "/accounts/signup/",
                    "secondary_cta_text": "Chat With Us",
                    "secondary_cta_url": "/about/"
                })
            ]
        },
        {
            "title": "Pricing & Plans",
            "slug": "pricing",
            "hero_tagline": "Pricing",
            "hero_title": "Simple, fair pricing.",
            "hero_subtitle": "No hidden device fees. No complex throughput math. Just the features you need to scale your industrial monitoring.",
            "body": [
                ("pricing_tiers", {
                    "title": "Simple, fair pricing.",
                    "subtitle": "No hidden device fees. No complex throughput math.",
                    "tiers": [
                        {
                            "name": "Starter",
                            "badge": "SME Entry Level",
                            "price": "$99",
                            "period": "/mo",
                            "features": ["1 Edge Gateway", "5 Monitored Devices", "7-Day Data History", "Standard Support"],
                            "cta_text": "Get Started",
                            "cta_url": "/accounts/signup/",
                            "is_popular": False
                        },
                        {
                            "name": "Professional",
                            "badge": "Most Popular",
                            "price": "$299",
                            "period": "/mo",
                            "features": ["3 Edge Gateways", "20 Monitored Devices", "30-Day Data History", "AI Data Assistant", "Logic Automations"],
                            "cta_text": "Talk to Sales",
                            "cta_url": "/accounts/signup/",
                            "is_popular": True
                        },
                        {
                            "name": "Business",
                            "badge": "Enterprise Ready",
                            "price": "$699",
                            "period": "/mo",
                            "features": ["10 Edge Gateways", "100 Monitored Devices", "1-Year Data History", "White-label Option"],
                            "cta_text": "Contact Sales",
                            "cta_url": "/about/",
                            "is_popular": False
                        }
                    ]
                }),
                ("pricing_comparison", {
                    "title": "Compare Features",
                    "features": [
                        {"name": "TimescaleDB Telemetry", "starter": "check", "professional": "check", "business": "check"},
                        {"name": "AI Data Assistant", "starter": "-", "professional": "check", "business": "check"},
                        {"name": "Logic & Automations", "starter": "View-only", "professional": "check", "business": "check"},
                        {"name": "Maintenance Ticketing", "starter": "Reactive Only", "professional": "check", "business": "check"},
                        {"name": "Kiosk & Shared Links", "starter": "1 Link", "professional": "10 Links", "business": "Unlimited"}
                    ]
                }),
                ("faq_accordion", {
                    "title": "Frequently Asked Questions",
                    "faqs": [
                        {
                            "question": "Do I need dedicated hardware to use Novena?",
                            "answer": "You can use Novena with your own hardware using our open-source edge gateway, or purchase a pre-configured Novena Gateway for zero-config setup. We support Modbus, MQTT, and Siemens S7 protocols out of the box."
                        },
                        {
                            "question": "Is there a limit on how much data I can store?",
                            "answer": (
                                "Your plan controls the telemetry history you can view and export, such as 7, 30, "
                                "or 90 days. Novena may retain telemetry internally for up to the global database "
                                "retention window, currently 90 days, so upgrades can make already-retained "
                                "history visible."
                            )
                        },
                        {
                            "question": "Can I cancel my subscription at any time?",
                            "answer": "Yes. Novena is a month-to-month service. You can cancel at any time from your dashboard settings without any cancellation fees."
                        }
                    ]
                })
            ]
        },
        {
            "title": "About Novena",
            "slug": "about",
            "hero_tagline": "Mission",
            "hero_title": "Smart factories,<br/><span class=\"text-indigo-600\">better world.</span>",
            "hero_subtitle": "Novena was born from a simple observation: modern software hadn't reached the factory floor. We're on a mission to democratize industrial intelligence for SMEs across ASEAN.",
            "body": [
                ("html", """
<section class="py-32 bg-base-200/50">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div class="grid grid-cols-1 md:grid-cols-3 gap-12">
            <div class="space-y-4">
                <div class="w-10 h-10 bg-indigo-500 rounded-xl flex items-center justify-center text-white"><i class="fa fa-heart"></i></div>
                <h3 class="text-xl font-black text-base-content">Simplicity First</h3>
                <p class="text-sm text-base-content/60 leading-relaxed">Industrial software shouldn't require a 3-week training course. We build tools that technicians actually enjoy using.</p>
            </div>
            <div class="space-y-4">
                <div class="w-10 h-10 bg-emerald-500 rounded-xl flex items-center justify-center text-white"><i class="fa fa-shield-check"></i></div>
                <h3 class="text-xl font-black text-base-content">Data Sovereignty</h3>
                <p class="text-sm text-base-content/60 leading-relaxed">Your data belongs to you. We provide open exports and transparent storage policies, ensuring no vendor lock-in.</p>
            </div>
            <div class="space-y-4">
                <div class="w-10 h-10 bg-amber-500 rounded-xl flex items-center justify-center text-white"><i class="fa fa-microchip"></i></div>
                <h3 class="text-xl font-black text-base-content">Edge-Enabled</h3>
                <p class="text-sm text-base-content/60 leading-relaxed">We believe the future is hybrid. Powerful cloud analytics paired with robust, offline-resilient edge hardware.</p>
            </div>
        </div>
    </div>
</section>

<section class="py-32 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
    <div class="bg-base-content rounded-4xl p-12 lg:p-20 relative overflow-hidden">
        <div class="absolute top-0 right-0 w-64 h-64 bg-indigo-500/10 blur-3xl -translate-y-1/2 translate-x-1/2"></div>
        <div class="lg:grid lg:grid-cols-2 lg:gap-16 items-center relative z-10">
            <div>
                <h2 class="text-3xl font-black text-white font-heading mb-6">Let's build the future together.</h2>
                <p class="text-white/60 mb-8 leading-relaxed">Whether you're an OEM looking to add cloud features to your machines, or a facility manager starting your digital journey, we're here to help.</p>
                <div class="space-y-4">
                    <div class="flex items-center text-white/80"><i class="fa fa-envelope w-6 text-indigo-400"></i> ${CONTACT_EMAIL}</div>
                    <div class="flex items-center text-white/80"><i class="fa fa-location-dot w-6 text-indigo-400"></i> One North, Singapore</div>
                </div>
            </div>
            <div class="mt-12 lg:mt-0">
                <form class="space-y-4">
                    <input type="text" placeholder="Name" class="input input-lg bg-white/5 border-white/10 text-white w-full rounded-2xl focus:border-indigo-500" />
                    <input type="email" placeholder="Email" class="input input-lg bg-white/5 border-white/10 text-white w-full rounded-2xl focus:border-indigo-500" />
                    <textarea placeholder="Message" class="textarea textarea-lg bg-white/5 border-white/10 text-white w-full rounded-2xl h-32 focus:border-indigo-500"></textarea>
                    <button class="btn btn-primary btn-block h-14 rounded-2xl font-black uppercase tracking-widest">Send Inquiry</button>
                </form>
            </div>
        </div>
    </div>
</section>
""")
            ]
        }
    ]

    for pdata in subpages_data:
        if not MarketingStandardPage.objects.filter(slug=pdata["slug"]).exists():
            marketing_page_ct, _ = ContentType.objects.get_or_create(
                app_label='content', model='marketingstandardpage'
            )
            sp = MarketingStandardPage(
                title=pdata["title"],
                slug=pdata["slug"],
                content_type_id=marketing_page_ct.id,
                locale_id=homepage.locale_id,
                live=True,
                hero_tagline=pdata["hero_tagline"],
                hero_title=pdata["hero_title"],
                hero_subtitle=pdata["hero_subtitle"],
                body=pdata["body"]
            )
            homepage.add_child(instance=sp)

    # Update content type mapping after Wagtail tree operations finish.
    try:
        from django.contrib.contenttypes.models import ContentType
        from django.db import connection
        nodeflow_homepage_ct = ContentType.objects.get(app_label="content", model="nodeflowhomepage")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE wagtailcore_page SET content_type_id = %s WHERE id = 2;", [nodeflow_homepage_ct.id])
    except Exception as e:
        print(f"Error updating homepage content type in migration: {e}")

def remove_wagtail_pages(apps, schema_editor):
    MarketingStandardPage = apps.get_model('content', 'MarketingStandardPage')
    from wagtail.models import Page
    from django.contrib.contenttypes.models import ContentType
    from django.db import connection

    # Delete subpages
    MarketingStandardPage.objects.filter(slug__in=["product", "solutions", "pricing", "about"]).delete()

    # Revert homepage ID=2 to base HomePage
    try:
        homepage_ct = ContentType.objects.get(app_label='content', model='homepage')
        page = Page.objects.get(id=2)
        page.content_type = homepage_ct
        page.save()
        
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM content_nodeflowhomepage WHERE page_ptr_id = 2;")
            cursor.execute("INSERT INTO content_homepage (page_ptr_id) VALUES (2);")
    except Exception as e:
        print(f"Error reverting homepage: {e}")

class Migration(migrations.Migration):

    dependencies = [
        ("content", "0006_marketingstandardpage_nodeflowhomepage"),
    ]

    operations = [
        migrations.RunPython(create_wagtail_pages, remove_wagtail_pages),
    ]

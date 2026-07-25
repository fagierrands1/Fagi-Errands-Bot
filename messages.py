from whatsapp import send_message, send_list, send_buttons, send_image


async def send_welcome(phone: str):
    await send_list(
        phone,
        "👋 *Welcome to Fagi Errands!*\n\nSafe, reliable & fast courier services across Kenya.",
        "Main Menu",
        [{"title": "Options", "rows": [
            {"id": "1", "title": "Book a New Errand"},
            {"id": "2", "title": "Check Errand Status"},
            {"id": "3", "title": "Talk to an Agent"},
            {"id": "4", "title": "Download Our App"},
            {"id": "5", "title": "Visit Our Website"},
            {"id": "6", "title": "Rates & FAQs"},
        ]}]
    )


async def send_load_type(phone: str):
    await send_buttons(phone,
        "📦 *Step 1 of 3 — Select Load Type*",
        [
            ("1", "Standard Parcel"),
            ("2", "Cargo / Large Items"),
            ("4", "Cancel & End Session"),
        ]
    )


async def send_pickup_prompt(phone: str):
    await send_message(phone,
        "📍 *Where should we pick up your parcel?*\n\n"
        "📌 Attach a location pin  —or—  type the area name (e.g. Kibera, Westlands)\n\n"
        "Type *cancel* to end the session."
    )


async def send_delivery_prompt(phone: str):
    await send_message(phone,
        "🏁 *Where should we deliver your parcel?*\n\n"
        "📌 Attach a location pin  —or—  type the area name (e.g. CBD, Karen)\n\n"
        "Type *cancel* to end the session."
    )


async def send_session_ended(phone: str):
    await send_message(phone,
        "✅ Session ended. Your order has been cancelled.\n"
        "Reply anything to return to the main menu."
    )


async def send_rates(phone: str):
    await send_message(phone,
        "📋 *Rates & FAQs*\n\n"
        "🛵 KES 200 for the first 7.5 km\n"
        "🛵 KES 23 per km after that\n\n"
        "📦 Standard Parcel: docs, food, clothes, electronics (<15kg)\n"
        "📦 Cargo: large boxes, furniture, bulky items\n\n"
        "💵 Payment: Cash on pickup or delivery.\n\n"
        "1 - Book a New Errand\n"
        "3 - Talk to an Agent\n"
        "0 - Return to Main Menu"
    )


async def send_agent_handoff(phone: str):
    import os
    agent_phone = os.getenv("AGENT_PHONE")
    await send_message(phone,
        "🤝 *Connecting you to an Agent...*\n\n"
        "Our customer care team has been notified to call you right away!\n\n"
        f"📞 You can also reach us directly at:\n+{agent_phone}"
    )


async def send_no_status(phone: str):
    await send_buttons(phone,
        "🔍 *No active errands found.*\nYou don't have an ongoing order right now.",
        [
            ("status_book", "🛵 Book an Errand"),
            ("status_home", "🏠 Main Menu"),
        ]
    )


async def send_receiver_prompt(phone: str):
    await send_message(phone,
        "📦 *Almost done! Enter item value & receiver phone:*\n\n"
        "Reply in this format:\n"
        "*Item Value (KES), Receiver Phone*\n\n"
        "Example:\n"
        "_5000, 0712345678_\n\n"
        "Or type *skip* to use your own details as the receiver.\n"
        "Type *cancel* to end the session."
    )


async def send_invalid(phone: str):
    await send_message(phone, "❓ Invalid option. Please tap a button or reply with one of the listed numbers.\n\nType *cancel* at any time to end the session.")

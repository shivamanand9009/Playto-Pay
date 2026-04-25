"""
Seed the database with 3 merchants and credit histories.
Run: python manage.py seed_merchants
"""
import uuid
from django.core.management.base import BaseCommand
from django.db import transaction
from payouts.models import Merchant, LedgerEntry


MERCHANT_SEED = [
    {
        'name': 'Arjun Electronics',
        'email': 'arjun@arjunelectronics.in',
        'bank_account_id': 'HDFC0001234567',
        'credits': [
            (250_000, 'Customer order #1001 — Samsung TV'),
            (89_999,  'Customer order #1002 — Wireless earbuds'),
            (45_000,  'Customer order #1003 — Phone case bundle'),
            (320_000, 'Customer order #1004 — Laptop'),
            (15_000,  'Customer order #1005 — USB-C hub'),
        ],
    },
    {
        'name': 'Priya Fashions',
        'email': 'priya@priyafashions.in',
        'bank_account_id': 'ICICI0009876543',
        'credits': [
            (12_000, 'Customer order #2001 — Silk saree'),
            (8_500,  'Customer order #2002 — Kurta set'),
            (22_000, 'Customer order #2003 — Bridal dupatta'),
            (6_000,  'Customer order #2004 — Cotton kurti'),
            (18_500, 'Customer order #2005 — Designer lehenga'),
        ],
    },
    {
        'name': 'Mumbai Bites',
        'email': 'ops@mumbaibites.in',
        'bank_account_id': 'AXIS0005551234',
        'credits': [
            (4_200,  'Customer order #3001 — Lunch delivery x 3'),
            (11_500, 'Customer order #3002 — Office catering'),
            (2_800,  'Customer order #3003 — Breakfast combo'),
            (35_000, 'Customer order #3004 — Wedding appetisers'),
            (7_600,  'Customer order #3005 — Dinner delivery x 5'),
        ],
    },
]


class Command(BaseCommand):
    help = 'Seed merchants with credit history'

    def handle(self, *args, **options):
        for seed in MERCHANT_SEED:
            with transaction.atomic():
                merchant, created = Merchant.objects.get_or_create(
                    email=seed['email'],
                    defaults={
                        'name': seed['name'],
                        'bank_account_id': seed['bank_account_id'],
                    }
                )
                if created:
                    for amount, desc in seed['credits']:
                        LedgerEntry.objects.create(
                            merchant=merchant,
                            amount_paise=amount,
                            entry_type=LedgerEntry.CREDIT,
                            description=desc,
                        )
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Created merchant '{merchant.name}' with {len(seed['credits'])} credits"
                        )
                    )
                else:
                    self.stdout.write(f"Merchant '{merchant.name}' already exists, skipping.")

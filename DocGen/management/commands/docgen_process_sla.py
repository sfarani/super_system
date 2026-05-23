from django.core.management.base import BaseCommand

from DocGen.services import process_sla_events


class Command(BaseCommand):
    help = "Process DocGen SLA reminders and escalations for active workflow stages."

    def handle(self, *args, **options):
        result = process_sla_events()
        self.stdout.write(
            self.style.SUCCESS(
                "checked={checked} reminders_sent={reminders_sent} escalations_sent={escalations_sent} "
                "notifications_sent={notifications_sent} notification_failures={notification_failures}".format(**result)
            )
        )

from django.core.management.base import BaseCommand
from django.core.management import call_command
from core.models import Course


class Command(BaseCommand):
    help = "Seed the database with development data."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Flush all data before seeding.",
        )

    def handle(self, *args, **options):
        if options["flush"]:
            call_command("flush", "--no-input")

        self._seed_courses()
        self.stdout.write(self.style.SUCCESS("Seeding complete."))

    def _seed_courses(self):
        courses = [
            {
                "code": "CS-301",
                "defaults": {
                    "name": "Software Engineering and Design",
                    "major": "Computer Science",
                },
            },
            {
                "code": "CS-401",
                "defaults": {"name": "Database Systems", "major": "Computer Science"},
            },
            {
                "code": "MAT-200",
                "defaults": {
                    "name": "Mathematics for Computer Science",
                    "major": "Mathematics",
                },
            },
        ]
        for entry in courses:
            course, created = Course.objects.update_or_create(
                code=entry["code"],
                defaults=entry["defaults"],
            )
            action = "created" if created else "updated"
            self.stdout.write(f"  Course {action}: {course.name}")

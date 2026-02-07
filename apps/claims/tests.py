from django.test import TestCase
from apps.claims.models import Case, Client, InvolvedVehicle
from apps.claims.signals import update_or_create_vehicle

class SignalVehicleTestCase(TestCase):
    def setUp(self):
        self.client = Client.objects.create(phone_number="123")
        self.case = Case.objects.create(client=self.client)

    def test_create_vehicle(self):
        # Test creating a new vehicle with guilt info
        update_or_create_vehicle(
            self.case,
            role_identifier="A",
            license_plate="B123AAA",
            vin="VIN123",
            driver_name="Sofer A",
            is_guilty_verdict="Vehicul A"
        )

        v = InvolvedVehicle.objects.get(license_plate="B123AAA")
        self.assertEqual(v.vin_number, "VIN123")
        self.assertEqual(v.driver_name, "Sofer A")
        self.assertTrue(v.is_offender)

    def test_create_vehicle_not_guilty(self):
        # Test creating a vehicle that is NOT guilty
        update_or_create_vehicle(
            self.case,
            role_identifier="B",
            license_plate="B999ZZZ",
            vin="VIN999",
            driver_name="Sofer B",
            is_guilty_verdict="Vehicul A"
        )

        v = InvolvedVehicle.objects.get(license_plate="B999ZZZ")
        self.assertFalse(v.is_offender)

    def test_update_vehicle_no_overwrite_guilt(self):
        # 1. Create a vehicle that is explicitly GUILTY
        v = InvolvedVehicle.objects.create(
            case=self.case,
            license_plate="B123AAA",
            is_offender=True
        )

        # 2. Update it with a document that HAS NO verdict (e.g. Talon)
        # is_guilty_verdict is None or empty dict
        update_or_create_vehicle(
            self.case,
            role_identifier="A",
            license_plate="B123AAA",
            vin="VIN_NEW",
            driver_name="Sofer A",
            is_guilty_verdict=None
        )

        v.refresh_from_db()
        self.assertEqual(v.vin_number, "VIN_NEW")
        # Ensure is_offender is STILL True
        self.assertTrue(v.is_offender)

    def test_update_vehicle_overwrite_guilt_if_conclusive(self):
        # 1. Create a vehicle that is NOT guilty initially
        v = InvolvedVehicle.objects.create(
            case=self.case,
            license_plate="B123AAA",
            is_offender=False
        )

        # 2. Update with a document that says it IS guilty
        update_or_create_vehicle(
            self.case,
            role_identifier="A",
            license_plate="B123AAA",
            vin="VIN123",
            driver_name="Sofer A",
            is_guilty_verdict="Vehicul A"
        )

        v.refresh_from_db()
        # Should now be True
        self.assertTrue(v.is_offender)

    def test_partial_update_no_empty_overwrite(self):
        # 1. Create vehicle with full data
        v = InvolvedVehicle.objects.create(
            case=self.case,
            license_plate="B123AAA",
            vin_number="VIN_ORIGINAL",
            driver_name="Sofer Original"
        )

        # 2. Update with missing VIN (None)
        update_or_create_vehicle(
            self.case,
            role_identifier="A",
            license_plate="B123AAA",
            vin=None,
            driver_name="Sofer Nou",
            is_guilty_verdict=None
        )

        v.refresh_from_db()
        # VIN should NOT be overwritten with empty string
        self.assertEqual(v.vin_number, "VIN_ORIGINAL")
        self.assertEqual(v.driver_name, "Sofer Nou")

    def test_create_vehicle_with_insurance(self):
        # Test creating a vehicle with insurance company
        update_or_create_vehicle(
            self.case,
            role_identifier="A",
            license_plate="B123AAA",
            vin="VIN123",
            driver_name="Sofer A",
            is_guilty_verdict="Vehicul A",
            insurance_company="Compania X"
        )

        v = InvolvedVehicle.objects.get(license_plate="B123AAA")
        self.assertEqual(v.insurance_company_name, "Compania X")

    def test_update_vehicle_insurance(self):
        # 1. Create vehicle
        v = InvolvedVehicle.objects.create(
            case=self.case,
            license_plate="B123AAA",
            insurance_company_name="Old Company"
        )

        # 2. Update with new insurance
        update_or_create_vehicle(
            self.case,
            role_identifier="A",
            license_plate="B123AAA",
            vin=None,
            driver_name=None,
            is_guilty_verdict=None,
            insurance_company="New Company"
        )

        v.refresh_from_db()
        self.assertEqual(v.insurance_company_name, "New Company")

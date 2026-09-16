import pytest
from django.utils import timezone
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from rest_framework.test import APIClient
from django.contrib.auth.models import User
from rest_framework_simplejwt.tokens import RefreshToken
from core.models import Asset, Employee, CheckOut, OverdueNotice
from core.tasks import flag_overdue_checkouts
from core.services import checkout_asset, ConflictException


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser(username='admin', email='admin@example.com', password='password')


@pytest.fixture
def auth_client(api_client, admin_user):
    refresh = RefreshToken.for_user(admin_user)
    api_client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
    return api_client


@pytest.fixture
def test_employee(db):
    return Employee.objects.create(
        employee_code="EMP-TEST",
        full_name="Test Employee",
        email="test@example.com",
        is_active=True
    )


@pytest.fixture
def test_asset(db):
    return Asset.objects.create(
        asset_tag="TAG-TEST-01",
        name="Test Camera",
        category="CAMERA",
        status="AVAILABLE",
        purchase_date="2025-01-01"
    )


@pytest.mark.django_db
def test_health_check(api_client):
    response = api_client.get('/api/v1/health/')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


@pytest.mark.django_db
def test_three_checkout_limit(test_employee):
    for i in range(3):
        a = Asset.objects.create(
            asset_tag=f"LIMIT-TAG-{i}",
            name=f"Asset {i}",
            category="LAPTOP",
            status="CHECKED_OUT",
            purchase_date="2025-01-01"
        )
        CheckOut.objects.create(
            asset=a,
            employee=test_employee,
            due_at=timezone.now() + timedelta(days=5)
        )

    Asset.objects.create(
        asset_tag="LIMIT-TAG-4",
        name="Asset 4",
        category="LAPTOP",
        status="AVAILABLE",
        purchase_date="2025-01-01"
    )

    with pytest.raises(ConflictException):
        checkout_asset(
            asset_tag="LIMIT-TAG-4",
            employee_code="EMP-TEST",
            due_at=timezone.now() + timedelta(days=5)
        )


@pytest.mark.django_db(transaction=True)
def test_concurrency_checkout(test_asset):
    """Test that two simultaneous check-outs of the same asset result in exactly one success and one conflict."""
    Employee.objects.create(employee_code="EMP-C1", full_name="User One", email="u1@example.com", is_active=True)
    Employee.objects.create(employee_code="EMP-C2", full_name="User Two", email="u2@example.com", is_active=True)
    
    due = timezone.now() + timedelta(days=2)

    def attempt_checkout(code):
        try:
            return checkout_asset(asset_tag=test_asset.asset_tag, employee_code=code, due_at=due)
        except Exception as e:
            return e
        finally:
            from django.db import connections
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(attempt_checkout, "EMP-C1"),
            executor.submit(attempt_checkout, "EMP-C2")
        ]
        results = [f.result() for f in as_completed(futures)]

    from django.db import connections
    connections.close_all()

    successes = [r for r in results if isinstance(r, CheckOut)]
    conflicts = [r for r in results if isinstance(r, ConflictException)]

    assert len(successes) == 1
    assert len(conflicts) == 1


@pytest.mark.django_db
def test_employee_summary_endpoint(auth_client, test_employee, test_asset):
    now = timezone.now()
    # Item 1: returned item, hold duration = 8 days (from 10 days ago to 2 days ago)
    c1 = CheckOut.objects.create(
        asset=test_asset,
        employee=test_employee,
        due_at=now - timedelta(days=5),
        returned_at=now - timedelta(days=2)
    )
    CheckOut.objects.filter(id=c1.id).update(checked_out_at=now - timedelta(days=10))

    asset2 = Asset.objects.create(
        asset_tag="TAG-TEST-02",
        name="Test Laptop",
        category="LAPTOP",
        status="CHECKED_OUT",
        purchase_date="2025-01-01"
    )
    # Item 2: currently held, overdue (due 3 days ago)
    CheckOut.objects.create(
        asset=asset2,
        employee=test_employee,
        checked_out_at=now - timedelta(days=6),
        due_at=now - timedelta(days=3),
        returned_at=None
    )

    asset3 = Asset.objects.create(
        asset_tag="TAG-TEST-03",
        name="Test Sensor",
        category="SENSOR",
        status="CHECKED_OUT",
        purchase_date="2025-01-01"
    )
    # Item 3: currently held, not overdue (due in 5 days)
    CheckOut.objects.create(
        asset=asset3,
        employee=test_employee,
        checked_out_at=now - timedelta(days=1),
        due_at=now + timedelta(days=5),
        returned_at=None
    )

    response = auth_client.get(f'/api/v1/employees/{test_employee.employee_code}/summary/')
    assert response.status_code == 200
    data = response.json()
    assert data['lifetime_checkouts'] == 3
    assert data['currently_held'] == 2
    assert data['currently_overdue'] == 1
    assert data['mean_hold_duration_days'] == 8.0


@pytest.mark.django_db
def test_overdue_calculation_including_due_now(auth_client, test_employee):
    now = timezone.now()

    # Asset 1: overdue by 5 days
    a1 = Asset.objects.create(
        asset_tag="TAG-DUE-PAST",
        name="Past Due Asset",
        category="CAMERA",
        status="CHECKED_OUT",
        purchase_date="2025-01-01"
    )
    c1 = CheckOut.objects.create(
        asset=a1,
        employee=test_employee,
        checked_out_at=now - timedelta(days=10),
        due_at=now - timedelta(days=5),
        returned_at=None
    )

    # Asset 2: due exactly now
    a2 = Asset.objects.create(
        asset_tag="TAG-DUE-NOW",
        name="Due Now Asset",
        category="LAPTOP",
        status="CHECKED_OUT",
        purchase_date="2025-01-01"
    )
    c2 = CheckOut.objects.create(
        asset=a2,
        employee=test_employee,
        checked_out_at=now - timedelta(days=2),
        due_at=now,
        returned_at=None
    )

    # Asset 3: due in future (not overdue)
    a3 = Asset.objects.create(
        asset_tag="TAG-DUE-FUTURE",
        name="Future Due Asset",
        category="SENSOR",
        status="CHECKED_OUT",
        purchase_date="2025-01-01"
    )
    CheckOut.objects.create(
        asset=a3,
        employee=test_employee,
        checked_out_at=now - timedelta(days=1),
        due_at=now + timedelta(days=3),
        returned_at=None
    )

    # Asset 4: already returned (not overdue)
    a4 = Asset.objects.create(
        asset_tag="TAG-RETURNED",
        name="Returned Asset",
        category="VEHICLE",
        status="AVAILABLE",
        purchase_date="2025-01-01"
    )
    CheckOut.objects.create(
        asset=a4,
        employee=test_employee,
        checked_out_at=now - timedelta(days=8),
        due_at=now - timedelta(days=4),
        returned_at=now - timedelta(days=1)
    )

    response = auth_client.get('/api/v1/reports/overdue/')
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    # Ordered by due_at ascending: c1 first (5 days overdue), c2 second (due now, 0 days overdue)
    assert rows[0]['checkout_id'] == c1.id
    assert rows[0]['days_overdue'] == 5
    assert rows[1]['checkout_id'] == c2.id
    assert rows[1]['days_overdue'] == 0


@pytest.mark.django_db
def test_task_idempotency(test_employee, test_asset):
    test_asset.status = "CHECKED_OUT"
    test_asset.save()

    checkout = CheckOut.objects.create(
        asset=test_asset,
        employee=test_employee,
        due_at=timezone.now() - timedelta(days=2)
    )

    flag_overdue_checkouts()
    assert OverdueNotice.objects.filter(checkout=checkout).count() == 1

    flag_overdue_checkouts()
    assert OverdueNotice.objects.filter(checkout=checkout).count() == 1
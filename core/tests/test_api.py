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
        from django.db import connection
        try:
            return checkout_asset(asset_tag=test_asset.asset_tag, employee_code=code, due_at=due)
        except Exception as e:
            return e
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(attempt_checkout, "EMP-C1"),
            executor.submit(attempt_checkout, "EMP-C2")
        ]
        results = [f.result() for f in as_completed(futures)]

    successes = [r for r in results if isinstance(r, CheckOut)]
    conflicts = [r for r in results if isinstance(r, ConflictException)]

    assert len(successes) == 1
    assert len(conflicts) == 1


@pytest.mark.django_db
def test_employee_summary_endpoint(auth_client, test_employee, test_asset):
    CheckOut.objects.create(
        asset=test_asset,
        employee=test_employee,
        checked_out_at=timezone.now() - timedelta(days=10),
        due_at=timezone.now() - timedelta(days=5),
        returned_at=timezone.now() - timedelta(days=2)
    )
    
    response = auth_client.get(f'/api/v1/employees/{test_employee.employee_code}/summary/')
    assert response.status_code == 200
    data = response.json()
    assert data['lifetime_checkouts'] == 1
    assert data['currently_held'] == 0


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


@pytest.mark.django_db
def test_overdue_calculation_exact_now(auth_client, test_employee, test_asset):
    """Test overdue calculation including item due exactly now or in the past."""
    test_asset.status = "CHECKED_OUT"
    test_asset.save()

    now = timezone.now()
    # Checkout due exactly now
    checkout = CheckOut.objects.create(
        asset=test_asset,
        employee=test_employee,
        due_at=now
    )

    response = auth_client.get('/api/v1/reports/overdue/')
    assert response.status_code == 200
    report = response.json()
    assert len(report) >= 1
    assert any(r['checkout_id'] == checkout.id for r in report)


@pytest.mark.django_db
def test_return_already_returned_checkout_conflict(auth_client, test_employee, test_asset):
    """Rule 6: Returning an already-returned check-out -> 409 Conflict."""
    test_asset.status = "CHECKED_OUT"
    test_asset.save()

    checkout = CheckOut.objects.create(
        asset=test_asset,
        employee=test_employee,
        due_at=timezone.now() + timedelta(days=5)
    )

    # First return: 200 OK
    resp1 = auth_client.post(f'/api/v1/checkouts/{checkout.id}/return/', {}, format='json')
    assert resp1.status_code == 200

    # Second return: 409 Conflict
    resp2 = auth_client.post(f'/api/v1/checkouts/{checkout.id}/return/', {}, format='json')
    assert resp2.status_code == 409
    assert "already been returned" in resp2.json()['error']
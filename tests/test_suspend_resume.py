"""End-to-end tests for temporary suspension / resumption of approved grants.

Covers the suspend/resume endpoints, their shared revision arbitration with
approve/revoke/guard-band changes, effective-set membership, permanent
revocation while suspended, and historical snapshot reconstruction.
"""
from fastapi.testclient import TestClient  # noqa: F401  (kept for type hints)


def body(
    circle=(0, 0, 10),
    freq=(100, 110),
    start="2026-10-01T08:00:00Z",
    end="2026-10-01T10:00:00Z",
    name="Event",
):
    from factories import make_payload

    return make_payload(circle, freq, start, end, name)


def submit(client, name, **kw):
    return client.post("/api/applications", json=body(name=name, **kw)).json()["id"]


def approve(client, app_id, expected):
    r = client.post(
        f"/api/applications/{app_id}/approve",
        json={"expected_revision": expected},
    )
    assert r.status_code == 200, r.text
    return r.json()


class TestSuspend:
    def test_suspend_takes_grant_out_of_effective_set(self, client):
        a = submit(client, "A")
        b = submit(client, "B")  # overlaps A in every dimension
        approve(client, a, 0)
        # B cannot be approved while A is effective
        blocked = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 1}
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "interference"

        r = client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 1, "reason": "device outage"},
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["suspended"] is True
        assert out["revision"] == 2
        assert out["guard_band"] == 2.0
        assert out["previous_approval_revision"] == 1
        assert out["reason"] == "device outage"
        assert out["application"]["status"] == "suspended"
        assert out["application"]["approval_revision"] == 1

        # status and effective set reflect the suspension
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 2
        assert current["effective_application_ids"] == []

        # B now previews clean against the (empty) effective set
        prev = client.post(f"/api/applications/{b}/preview").json()
        assert prev["current_revision"] == 2
        assert prev["has_conflicts"] is False

        # available-ranges also adjudicates without the suspended grant
        ranges = client.post(
            f"/api/applications/{b}/available-ranges",
            json={"candidate_low_min": 90, "candidate_low_max": 130},
        ).json()
        assert ranges["available_ranges"] == [
            {"min": 90, "max": 130, "min_inclusive": True, "max_inclusive": True}
        ]

    def test_suspend_stale_revision_refuses_and_consumes_none(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        r = client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 0},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"] == {"expected_revision": 0, "current_revision": 1}
        # grant stays effective, revision unchanged
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 1
        assert current["effective_application_ids"] == [a]
        assert current["suspensions"] == []

    def test_suspend_unknown_id_404(self, client):
        r = client.post(
            "/api/applications/999/suspend", json={"expected_revision": 0}
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_suspend_pending_rejected(self, client):
        a = submit(client, "A")
        r = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 0}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_pending"
        assert r.json()["error"]["details"] == {"status": "pending"}

    def test_suspend_suspended_rejected_duplicate(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()
        # repeat suspension is a duplicate operation
        r = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 2}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_suspended"
        # no extra revision consumed
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 2
        )

    def test_suspend_revoked_rejected(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(f"/api/applications/{a}/revoke", json={}).raise_for_status()
        r = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 2}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_revoked"

    def test_approving_a_suspended_grant_is_rejected(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()
        r = client.post(
            f"/api/applications/{a}/approve", json={"expected_revision": 2}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_suspended"

    def test_suspended_member_rejects_batch(self, client):
        a = submit(client, "A")
        b = submit(client, "B", freq=(200, 201))
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()
        r = client.post(
            "/api/applications/batch-approve",
            json={"expected_revision": 2, "application_ids": [a, b]},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "not_pending"
        assert err["details"]["applications"] == [
            {
                "application_id": a,
                "status": "suspended",
                "approval_revision": 1,
            }
        ]
        assert client.get("/api/revision/current").json()["current_revision"] == 2


class TestResume:
    def test_resume_restores_grant_and_consumes_one_revision(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 1, "reason": "outage"},
        ).raise_for_status()

        r = client.post(
            f"/api/applications/{a}/resume",
            json={"expected_revision": 2, "reason": "fixed"},
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["resumed"] is True
        assert out["revision"] == 3
        assert out["guard_band"] == 2.0
        assert out["previous_approval_revision"] == 1
        assert out["reason"] == "fixed"
        assert out["application"]["status"] == "approved"

        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 3
        assert current["effective_application_ids"] == [a]
        # the restored grant blocks conflicting approvals again
        b = submit(client, "B")
        prev = client.post(f"/api/applications/{b}/preview").json()
        assert prev["current_revision"] == 3
        assert prev["has_conflicts"] is True

    def test_resume_with_interference_stays_suspended_no_revision(self, client):
        a = submit(client, "A")
        b = submit(client, "B")  # overlaps A in every dimension
        approve(client, a, 0)  # revision 1
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()  # revision 2
        # while A is suspended B becomes effective (revision 3)
        approve(client, b, 2)

        r = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 3}
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        assert err["details"]["current_revision"] == 3
        (conflict,) = err["details"]["conflicts"]
        assert conflict["application"]["id"] == b
        assert conflict["reason"] == "frequency_intersection"

        # A stays suspended; no revision consumed
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"
        current = client.get("/api/revision/current").json()
        assert current["current_revision"] == 3
        assert current["effective_application_ids"] == [b]
        assert current["resumptions"] == []

        # once B is out of the way, the same suspended A resumes successfully
        client.post(f"/api/applications/{b}/revoke", json={}).raise_for_status()
        ok = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 4}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["revision"] == 5
        assert client.get(f"/api/applications/{a}").json()["status"] == "approved"

    def test_resume_stale_revision_stays_suspended_no_revision(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()
        # another writer advances the revision (guard-band change at revision 3)
        client.post(
            "/api/config/guard-band",
            json={"guard_band": 1.0, "expected_revision": 2},
        ).raise_for_status()

        r = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 2}
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "stale_revision"
        assert err["details"] == {"expected_revision": 2, "current_revision": 3}
        assert client.get(f"/api/applications/{a}").json()["status"] == "suspended"

    def test_resume_unknown_id_404(self, client):
        r = client.post(
            "/api/applications/999/resume", json={"expected_revision": 0}
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"

    def test_resume_active_grant_rejected(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        r = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 1}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_suspended"
        assert r.json()["error"]["details"] == {"status": "approved"}

    def test_resume_pending_rejected(self, client):
        a = submit(client, "A")
        r = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 0}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "not_suspended"
        assert r.json()["error"]["details"] == {"status": "pending"}

    def test_resume_revoked_rejected(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()
        client.post(f"/api/applications/{a}/revoke", json={}).raise_for_status()

        r = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 3}
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "already_revoked"
        # revision unchanged by the refusal
        assert (
            client.get("/api/revision/current").json()["current_revision"] == 3
        )

    def test_resume_uses_live_guard_band(self, client):
        # A [100,110], B [112,120]: gap 2.  Compatible under band 2.0
        # (gap == band allowed), incompatible under band 3.0 (gap < band).
        a = submit(client, "A", freq=(100, 110))
        b = submit(client, "B", freq=(112, 120))
        approve(client, a, 0)
        approve(client, b, 1)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 2}
        ).raise_for_status()
        # widen the guard band while A is suspended (revision 4)
        client.post(
            "/api/config/guard-band",
            json={"guard_band": 3.0, "expected_revision": 3},
        ).raise_for_status()

        r = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 4}
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "interference"
        (conflict,) = err["details"]["conflicts"]
        assert conflict["application"]["id"] == b
        assert conflict["reason"] == "within_guard_band"
        assert conflict["trigger"]["minimum_frequency_gap"] == 2.0

        # tightening back allows the resume; the resumption snapshots band 2.0
        client.post(
            "/api/config/guard-band",
            json={"guard_band": 2.0, "expected_revision": 4},
        ).raise_for_status()
        ok = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 5}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["guard_band"] == 2.0
        snap = client.get("/api/revision/6").json()
        (resume_record,) = snap["resumptions"]
        assert resume_record == {
            "revision": 6,
            "application_id": a,
            "reason": "",
            "guard_band": 2.0,
            "created_at": resume_record["created_at"],
        }


class TestRevokeWhileSuspended:
    def test_revoke_suspended_grant_permanently(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()

        r = client.post(
            f"/api/applications/{a}/revoke", json={"reason": "cancelled"}
        )
        assert r.status_code == 200, r.text
        out = r.json()
        assert out["revoked"] is True
        assert out["revision"] == 3
        assert out["previous_approval_revision"] == 1
        assert out["previous_status"] == "suspended"
        assert out["reason"] == "cancelled"
        assert client.get(f"/api/applications/{a}").json()["status"] == "revoked"
        current = client.get("/api/revision/current").json()
        assert current["effective_application_ids"] == []

        # cannot resume or suspend or revoke again
        assert (
            client.post(
                f"/api/applications/{a}/resume", json={"expected_revision": 3}
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"/api/applications/{a}/suspend", json={"expected_revision": 3}
            ).status_code
            == 409
        )
        again = client.post(f"/api/applications/{a}/revoke", json={})
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "already_revoked"


class TestSuspendResumeHistory:
    def test_snapshots_restore_suspend_resume_and_effective_sets(self, client):
        a = submit(client, "A", freq=(100, 101))
        approve(client, a, 0)  # rev 1

        # revision 2: suspended
        client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 1, "reason": "outage 1"},
        ).raise_for_status()
        # revision 3: resumed
        client.post(
            f"/api/applications/{a}/resume",
            json={"expected_revision": 2, "reason": "fixed 1"},
        ).raise_for_status()
        # revision 4: suspended again
        client.post(
            f"/api/applications/{a}/suspend",
            json={"expected_revision": 3, "reason": "outage 2"},
        ).raise_for_status()

        # as-of revision 1: approved and effective
        snap1 = client.get("/api/revision/1").json()
        assert snap1["effective_application_ids"] == [a]
        assert snap1["suspensions"] == [] and snap1["resumptions"] == []
        assert snap1["approvals"][0]["status_as_of"] == "approved"

        # as-of revision 2: suspended
        snap2 = client.get("/api/revision/2").json()
        assert snap2["effective_application_ids"] == []
        approval = snap2["approvals"][0]
        assert approval["status_as_of"] == "suspended"
        assert approval["suspended_at_revision"] == 2
        assert "revoked_at_revision" not in approval
        (susp,) = snap2["suspensions"]
        assert susp["revision"] == 2 and susp["application_id"] == a
        assert susp["reason"] == "outage 1"

        # as-of revision 3: effective again
        snap3 = client.get("/api/revision/3").json()
        assert snap3["effective_application_ids"] == [a]
        assert snap3["approvals"][0]["status_as_of"] == "approved"
        assert len(snap3["suspensions"]) == 1 and len(snap3["resumptions"]) == 1
        assert snap3["resumptions"][0]["revision"] == 3
        assert snap3["resumptions"][0]["reason"] == "fixed 1"
        assert snap3["resumptions"][0]["guard_band"] == 2.0

        # as-of revision 4: suspended again, full event chain preserved
        snap4 = client.get("/api/revision/4").json()
        assert snap4["effective_application_ids"] == []
        assert snap4["approvals"][0]["status_as_of"] == "suspended"
        assert snap4["approvals"][0]["suspended_at_revision"] == 4
        assert [e["revision"] for e in snap4["suspensions"]] == [2, 4]
        assert [e["revision"] for e in snap4["resumptions"]] == [3]
        assert snap4["revocations"] == []

        # past snapshots are immutable once written
        assert client.get("/api/revision/2").json() == snap2

    def test_original_approval_event_is_not_rewritten(self, client):
        a = submit(client, "A", freq=(100, 101))
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()
        client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 2}
        ).raise_for_status()

        for revision in (1, 2, 3):
            snap = client.get(f"/api/revision/{revision}").json()
            approval = snap["approvals"][0]
            assert approval["revision"] == 1
            assert approval["guard_band"] == 2.0
            assert approval["application_id"] == a

    def test_snapshot_revoked_after_suspension(self, client):
        a = submit(client, "A")
        b = submit(client, "B", freq=(200, 201))
        approve(client, a, 0)
        approve(client, b, 1)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 2}
        ).raise_for_status()
        client.post(f"/api/applications/{a}/revoke", json={}).raise_for_status()

        snap = client.get("/api/revision/4").json()
        assert snap["effective_application_ids"] == [b]
        approval_a = snap["approvals"][0]
        assert approval_a["application_id"] == a
        assert approval_a["status_as_of"] == "revoked"
        assert approval_a["revoked_at_revision"] == 4
        assert "suspended_at_revision" not in approval_a
        assert [e["application_id"] for e in snap["suspensions"]] == [a]
        assert snap["resumptions"] == []
        assert [e["application_id"] for e in snap["revocations"]] == [a]

    def test_current_revision_lists_suspend_and_resume_events(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()
        client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 2}
        ).raise_for_status()

        current = client.get("/api/revision/current").json()
        assert [e["revision"] for e in current["suspensions"]] == [2]
        assert [e["revision"] for e in current["resumptions"]] == [3]
        assert current["suspensions"][0]["application_id"] == a
        assert current["resumptions"][0]["application_id"] == a


class TestSuspendResumeArbitration:
    def test_suspend_shares_revision_sequence(self, client):
        a = submit(client, "A", freq=(100, 101))
        b = submit(client, "B", freq=(200, 201))
        approve(client, a, 0)  # rev 1

        # suspend consumes the shared revision (rev 2)
        susp = client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        )
        assert susp.status_code == 200 and susp.json()["revision"] == 2

        # an approval based on the old revision is stale
        stale = client.post(
            f"/api/applications/{b}/approve", json={"expected_revision": 1}
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "stale_revision"

        # ...and on the fresh revision B can be approved while A is suspended
        ok = approve(client, b, 2)
        assert ok["revision"] == 3

        # resume based on the pre-approval revision is stale
        stale_resume = client.post(
            f"/api/applications/{a}/resume", json={"expected_revision": 2}
        )
        assert stale_resume.status_code == 409
        assert stale_resume.json()["error"]["code"] == "stale_revision"

    def test_status_filter_includes_suspended(self, client):
        a = submit(client, "A")
        approve(client, a, 0)
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 1}
        ).raise_for_status()

        rows = client.get(
            "/api/applications", params={"status": "suspended"}
        ).json()["applications"]
        assert [r["id"] for r in rows] == [a]
        assert client.get(
            "/api/applications", params={"status": "approved"}
        ).json()["applications"] == []

    def test_guard_band_preview_excludes_suspended_grants(self, client):
        # A and B with gap 2 (< 4) would clash under a proposed band 4, but
        # suspending A removes it from the effective set.
        a = submit(client, "A", freq=(100, 110))
        b = submit(client, "B", freq=(112, 120))
        approve(client, a, 0)
        approve(client, b, 1)
        before = client.post(
            "/api/config/guard-band/preview", json={"guard_band": 4.0}
        ).json()
        assert before["interfering_pair_count"] == 1

        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 2}
        ).raise_for_status()
        after = client.post(
            "/api/config/guard-band/preview", json={"guard_band": 4.0}
        ).json()
        assert after["effective_application_ids"] == [b]
        assert after["interfering_pairs"] == []
        assert after["current_revision"] == 3

    def test_repeated_cycles_and_final_revocation(self, client):
        a = submit(client, "A", freq=(100, 101))
        approve(client, a, 0)
        for expected in (1, 3, 5):
            r1 = client.post(
                f"/api/applications/{a}/suspend",
                json={"expected_revision": expected},
            )
            assert r1.status_code == 200, (expected, r1.text)
            r2 = client.post(
                f"/api/applications/{a}/resume",
                json={"expected_revision": expected + 1},
            )
            assert r2.status_code == 200, (expected, r2.text)
        # revisions 1..6 used; suspend once more then revoke
        client.post(
            f"/api/applications/{a}/suspend", json={"expected_revision": 7}
        ).raise_for_status()
        rev = client.post(f"/api/applications/{a}/revoke", json={})
        assert rev.status_code == 200 and rev.json()["revision"] == 9
        snap = client.get("/api/revision/9").json()
        assert [e["revision"] for e in snap["suspensions"]] == [2, 4, 6, 8]
        assert [e["revision"] for e in snap["resumptions"]] == [3, 5, 7]
        assert snap["approvals"][0]["status_as_of"] == "revoked"
        assert snap["effective_application_ids"] == []

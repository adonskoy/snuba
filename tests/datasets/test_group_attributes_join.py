import uuid
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, Mapping

import pytest
import simplejson as json

from snuba.datasets.entities.entity_key import EntityKey
from snuba.datasets.entities.factory import get_entity
from snuba.datasets.factory import get_dataset
from snuba.datasets.processors.search_issues_processor import SearchIssueEvent
from snuba.pipeline.composite import CompositeExecutionPipeline
from snuba.query.composite import CompositeQuery
from snuba.query.data_source.join import IndividualNode, JoinClause
from snuba.query.data_source.simple import Table
from snuba.query.query_settings import HTTPQuerySettings
from snuba.query.snql.parser import parse_snql_query
from snuba.utils.metrics.backends.dummy import DummyMetricsBackend
from snuba.web import QueryResult
from tests.base import BaseApiTest
from tests.fixtures import get_raw_event
from tests.helpers import write_unprocessed_events

CLICKHOUSE_DEFAULT_DATETIME = "1970-01-01T00:00:00+00:00"


def write_group_attribute_row(row: Mapping[str, Any]) -> None:
    groups_storage = get_entity(EntityKey.GROUP_ATTRIBUTES).get_writable_storage()
    groups_storage.get_table_writer().get_batch_writer(
        metrics=DummyMetricsBackend(strict=True)
    ).write([json.dumps(row).encode("utf-8")])


def _convert_clickhouse_datetime_str(str: str) -> str:
    return (
        datetime.strptime(str, "%Y-%m-%d %H:%M:%S")
        .replace(microsecond=0, tzinfo=timezone.utc)
        .isoformat()
    )


def _clickhouse_datetime_str(date_time: datetime) -> str:
    return date_time.strftime("%Y-%m-%d %H:%M:%S")


class TestEventsGroupAttributes(BaseApiTest):
    @pytest.fixture(autouse=True)
    def setup_fixture(self, clickhouse_db, redis_db):
        self.app.post = partial(self.app.post, headers={"referer": "test"})
        self.event = get_raw_event()
        self.project_id = self.event["project_id"]
        self.base_time = datetime.utcnow().replace(
            second=0, microsecond=0, tzinfo=timezone.utc
        ) - timedelta(minutes=90)
        self.next_time = self.base_time + timedelta(minutes=95)

        self.events_storage = get_entity(EntityKey.EVENTS).get_writable_storage()
        write_unprocessed_events(self.events_storage, [self.event])

        self.initial_group_attributes = {
            "deleted": False,
            "project_id": self.project_id,
            "group_id": self.event["group_id"],
            "group_status": 0,
            "group_substatus": 7,
            "group_first_seen": _clickhouse_datetime_str(self.base_time),
            "group_num_comments": 0,
            "assignee_user_id": 1,
            "assignee_team_id": 2,
            "owner_suspect_commit_user_id": 3,
            "owner_ownership_rule_user_id": 4,
            "owner_ownership_rule_team_id": 5,
            "owner_codeowners_user_id": 6,
            "owner_codeowners_team_id": 7,
            "message_timestamp": _clickhouse_datetime_str(self.base_time),
            "partition": 1,
            "offset": 1,
        }

        write_group_attribute_row(self.initial_group_attributes)

    def query_events_joined_group_attributes(self):
        query_template = (
            "MATCH (e: events) -[attributes]-> (g: group_attributes) "
            "SELECT e.event_id, "
            "g.group_id, g.group_status, g.group_substatus, "
            "g.group_first_seen, "
            "g.group_num_comments, "
            "g.assignee_user_id, "
            "g.assignee_team_id, "
            "g.owner_suspect_commit_user_id, "
            "g.owner_ownership_rule_user_id, "
            "g.owner_ownership_rule_team_id, "
            "g.owner_codeowners_user_id, "
            "g.owner_codeowners_team_id WHERE "
            "e.project_id = %(project_id)s AND "
            "g.project_id = %(project_id)s AND "
            "g.group_id = %(group_id)s AND "
            "e.timestamp >= toDateTime('%(btime)s') AND "
            "e.timestamp < toDateTime('%(ntime)s')"
        )
        query_body = query_template % {
            "project_id": self.project_id,
            "group_id": self.event["group_id"],
            "btime": self.base_time,
            "ntime": self.next_time,
        }

        def assert_joined_final(
            clickhouse_query,
            query_settings,
            reader,
        ) -> QueryResult:
            assert isinstance(clickhouse_query, CompositeQuery)
            assert isinstance(clickhouse_query.get_from_clause(), JoinClause)
            right_node = clickhouse_query.get_from_clause().right_node
            assert isinstance(right_node.data_source.get_from_clause(), Table)
            # make sure we're explicitly applying FINAL when querying on group_attributes table
            # so deduplication happens when we join the entity from events -> group_attributes
            assert right_node.data_source.get_from_clause().final
            assert right_node.data_source.get_from_clause().table_name.startswith(
                "group_attributes"
            )

            return QueryResult(
                {"data": []},
                {"stats": {}, "sql": "", "experiments": {}},
            )

        query, _ = parse_snql_query(str(query_body), get_dataset("events"))
        CompositeExecutionPipeline(
            query, HTTPQuerySettings(), assert_joined_final
        ).execute()

        return self.app.post(
            "/events/snql",
            data=json.dumps({"dataset": "events", "query": query_body}),
        )

    @pytest.mark.clickhouse_db
    @pytest.mark.redis_db
    def test_group_attributes_join(self) -> None:
        response = self.query_events_joined_group_attributes()
        data = json.loads(response.data)
        assert response.status_code == 200
        assert (
            data["data"][0].items()
            == {
                "e.event_id": self.event["event_id"],
                "g.group_id": self.initial_group_attributes["group_id"],
                "g.group_status": self.initial_group_attributes["group_status"],
                "g.group_substatus": self.initial_group_attributes["group_substatus"],
                "g.group_first_seen": _convert_clickhouse_datetime_str(
                    self.initial_group_attributes["group_first_seen"]
                ),
                "g.group_num_comments": self.initial_group_attributes[
                    "group_num_comments"
                ],
                "g.assignee_user_id": self.initial_group_attributes["assignee_user_id"],
                "g.assignee_team_id": self.initial_group_attributes["assignee_team_id"],
                "g.owner_suspect_commit_user_id": self.initial_group_attributes[
                    "owner_suspect_commit_user_id"
                ],
                "g.owner_ownership_rule_user_id": self.initial_group_attributes[
                    "owner_ownership_rule_user_id"
                ],
                "g.owner_ownership_rule_team_id": self.initial_group_attributes[
                    "owner_ownership_rule_team_id"
                ],
                "g.owner_codeowners_user_id": self.initial_group_attributes[
                    "owner_codeowners_user_id"
                ],
                "g.owner_codeowners_team_id": self.initial_group_attributes[
                    "owner_codeowners_team_id"
                ],
            }.items()
        )

    @pytest.mark.clickhouse_db
    @pytest.mark.redis_db
    def test_group_attributes_join_after_delete(self) -> None:
        delete_row = self.initial_group_attributes.copy()
        delete_row.update({"deleted": True})
        write_group_attribute_row(delete_row)
        response_after_delete = self.query_events_joined_group_attributes()
        data_after = json.loads(response_after_delete.data)
        assert response_after_delete.status_code == 200
        assert (
            data_after["data"][0].items()
            == {
                "e.event_id": self.event["event_id"],
                # values joined from group_attributes below should be 'null' since we 'deleted' the
                # existing group_attributes row that joins to the events but the projected values seems to
                # take on some defaults that are non-null since the columns themselves are not nullable
                "g.group_id": 0,  # 0 is a sentinel value indicating the left join on group_attributes returns no data
                "g.group_status": 0,
                "g.group_substatus": None,
                "g.group_first_seen": CLICKHOUSE_DEFAULT_DATETIME,
                "g.group_num_comments": 0,
                "g.assignee_user_id": None,
                "g.assignee_team_id": None,
                "g.owner_suspect_commit_user_id": None,
                "g.owner_ownership_rule_user_id": None,
                "g.owner_ownership_rule_team_id": None,
                "g.owner_codeowners_user_id": None,
                "g.owner_codeowners_team_id": None,
            }.items()
        )


class TestSearchIssuesGroupAttributes(BaseApiTest):
    @pytest.fixture(autouse=True)
    def setup_fixture(self, clickhouse_db, redis_db):
        self.app.post = partial(self.app.post, headers={"referer": "test"})

        self.base_time = datetime.utcnow().replace(
            second=0, microsecond=0, tzinfo=timezone.utc
        ) - timedelta(minutes=90)
        self.next_time = self.base_time + timedelta(minutes=95)
        self.occurrence = self.get_search_issue_occurrence(self.base_time)
        self.project_id = self.occurrence["project_id"]

        self.search_issues_storage = get_entity(
            EntityKey.SEARCH_ISSUES
        ).get_writable_storage()
        write_unprocessed_events(self.search_issues_storage, [self.occurrence])

        self.initial_group_attributes = {
            "deleted": False,
            "project_id": self.project_id,
            "group_id": self.occurrence["group_id"],
            "group_status": 0,
            "group_substatus": 7,
            "group_first_seen": _clickhouse_datetime_str(self.base_time),
            "group_num_comments": 0,
            "assignee_user_id": 1,
            "assignee_team_id": 2,
            "owner_suspect_commit_user_id": 3,
            "owner_ownership_rule_user_id": 4,
            "owner_ownership_rule_team_id": 5,
            "owner_codeowners_user_id": 6,
            "owner_codeowners_team_id": 7,
            "message_timestamp": _clickhouse_datetime_str(self.base_time),
            "partition": 1,
            "offset": 1,
        }

        write_group_attribute_row(self.initial_group_attributes)

    def get_search_issue_occurrence(self, sent_ts: datetime) -> SearchIssueEvent:
        return {
            "project_id": 1,
            "organization_id": 2,
            "group_id": 3,
            "event_id": str(uuid.uuid4()),
            "retention_days": 90,
            "primary_hash": str(uuid.uuid4()),
            "datetime": sent_ts.replace(microsecond=1, tzinfo=None).isoformat() + "Z",
            "platform": "other",
            "message": "something",
            "data": {
                "received": sent_ts.timestamp(),
            },
            "occurrence_data": {
                "id": str(uuid.uuid4()),
                "type": 1,
                "issue_title": "search me",
                "fingerprint": ["one", "two"],
                "detection_time": sent_ts.timestamp(),
            },
        }

    def query_search_issues_joined_group_attributes(self):
        query_template = (
            "MATCH (s: search_issues) -[attributes]-> (g: group_attributes) "
            "SELECT s.occurrence_id, "
            "g.group_id, g.group_status, g.group_substatus, "
            "g.group_first_seen, "
            "g.group_num_comments, "
            "g.assignee_user_id, "
            "g.assignee_team_id, "
            "g.owner_suspect_commit_user_id, "
            "g.owner_ownership_rule_user_id, "
            "g.owner_ownership_rule_team_id, "
            "g.owner_codeowners_user_id, "
            "g.owner_codeowners_team_id WHERE "
            "s.project_id = %(project_id)s AND "
            "g.project_id = %(project_id)s AND "
            "g.group_id = %(group_id)s AND "
            "s.timestamp >= toDateTime('%(btime)s') AND "
            "s.timestamp < toDateTime('%(ntime)s')"
        )
        query_body = query_template % {
            "project_id": self.project_id,
            "group_id": self.occurrence["group_id"],
            "btime": self.base_time,
            "ntime": self.next_time,
        }

        def assert_joined_final(
            clickhouse_query,
            query_settings,
            reader,
        ) -> QueryResult:
            assert isinstance(clickhouse_query, CompositeQuery)
            assert isinstance(clickhouse_query.get_from_clause(), JoinClause)
            left_node = clickhouse_query.get_from_clause().left_node
            assert isinstance(left_node, IndividualNode)
            assert isinstance(left_node.data_source.get_from_clause(), Table)
            assert not left_node.data_source.get_from_clause().final
            assert left_node.data_source.get_from_clause().table_name.startswith(
                "search_issues"
            )
            right_node = clickhouse_query.get_from_clause().right_node
            assert isinstance(right_node.data_source.get_from_clause(), Table)
            # make sure we're explicitly applying FINAL when querying on group_attributes table
            # so deduplication happens when we join the entity from events -> group_attributes
            assert right_node.data_source.get_from_clause().final
            assert right_node.data_source.get_from_clause().table_name.startswith(
                "group_attributes"
            )

            return QueryResult(
                {"data": []},
                {"stats": {}, "sql": "", "experiments": {}},
            )

        query, _ = parse_snql_query(str(query_body), get_dataset("search_issues"))
        CompositeExecutionPipeline(
            query, HTTPQuerySettings(), assert_joined_final
        ).execute()

        return self.app.post(
            "/search_issues/snql",
            data=json.dumps({"dataset": "search_issues", "query": query_body}),
        )

    @pytest.mark.clickhouse_db
    @pytest.mark.redis_db
    def test_group_attributes_join(self) -> None:
        response = self.query_search_issues_joined_group_attributes()
        data = json.loads(response.data)
        assert response.status_code == 200
        assert (
            data["data"][0].items()
            == {
                "s.occurrence_id": uuid.UUID(
                    self.occurrence["occurrence_data"]["id"]
                ).hex,
                "g.group_id": self.initial_group_attributes["group_id"],
                "g.group_status": self.initial_group_attributes["group_status"],
                "g.group_substatus": self.initial_group_attributes["group_substatus"],
                "g.group_first_seen": _convert_clickhouse_datetime_str(
                    self.initial_group_attributes["group_first_seen"]
                ),
                "g.group_num_comments": self.initial_group_attributes[
                    "group_num_comments"
                ],
                "g.assignee_user_id": self.initial_group_attributes["assignee_user_id"],
                "g.assignee_team_id": self.initial_group_attributes["assignee_team_id"],
                "g.owner_suspect_commit_user_id": self.initial_group_attributes[
                    "owner_suspect_commit_user_id"
                ],
                "g.owner_ownership_rule_user_id": self.initial_group_attributes[
                    "owner_ownership_rule_user_id"
                ],
                "g.owner_ownership_rule_team_id": self.initial_group_attributes[
                    "owner_ownership_rule_team_id"
                ],
                "g.owner_codeowners_user_id": self.initial_group_attributes[
                    "owner_codeowners_user_id"
                ],
                "g.owner_codeowners_team_id": self.initial_group_attributes[
                    "owner_codeowners_team_id"
                ],
            }.items()
        )

    @pytest.mark.clickhouse_db
    @pytest.mark.redis_db
    def test_group_attributes_join_after_delete(self) -> None:
        delete_row = self.initial_group_attributes.copy()
        delete_row.update({"deleted": True})
        write_group_attribute_row(delete_row)
        response_after_delete = self.query_search_issues_joined_group_attributes()
        data_after = json.loads(response_after_delete.data)
        assert response_after_delete.status_code == 200
        assert (
            data_after["data"][0].items()
            == {
                "s.occurrence_id": uuid.UUID(
                    self.occurrence["occurrence_data"]["id"]
                ).hex,
                # values joined from group_attributes below should be 'null' since we 'deleted' the
                # existing group_attributes row that joins to the events but the projected values seems to
                # take on some defaults that are non-null since the columns themselves are not nullable
                "g.group_id": 0,
                # 0 is a sentinel value indicating the left join on group_attributes returns no data
                "g.group_status": 0,
                "g.group_substatus": None,
                "g.group_first_seen": CLICKHOUSE_DEFAULT_DATETIME,
                "g.group_num_comments": 0,
                "g.assignee_user_id": None,
                "g.assignee_team_id": None,
                "g.owner_suspect_commit_user_id": None,
                "g.owner_ownership_rule_user_id": None,
                "g.owner_ownership_rule_team_id": None,
                "g.owner_codeowners_user_id": None,
                "g.owner_codeowners_team_id": None,
            }.items()
        )

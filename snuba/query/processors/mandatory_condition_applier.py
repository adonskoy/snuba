from snuba.clickhouse.processors import QueryProcessor
from snuba.clickhouse.query import Query
from snuba.query.conditions import combine_and_conditions
from snuba.request.request_settings import RequestSettings


class MandatoryConditionApplier(QueryProcessor):

    """
    Obtains mandatory conditions from a Query object’s underlying storage
    and applies them to the query.
    """

    def process_query(self, query: Query, request_settings: RequestSettings) -> None:

        mandatory_conditions = query.get_from_clause().mandatory_conditions

        if len(mandatory_conditions) > 0:
            query.add_condition_to_ast(combine_and_conditions(mandatory_conditions))

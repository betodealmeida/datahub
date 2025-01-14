import logging
import time
from typing import Optional, Union

from databricks.sdk import WorkspaceClient
from databricks.sdk.core import DatabricksError
from databricks.sdk.service._internal import Wait
from databricks.sdk.service.catalog import TableInfo
from databricks.sdk.service.sql import (
    ExecuteStatementResponse,
    GetStatementResponse,
    GetWarehouseResponse,
    StatementState,
    StatementStatus,
)
from databricks_cli.unity_catalog.api import UnityCatalogApi

from datahub.ingestion.source.unity.proxy_types import (
    ColumnProfile,
    TableProfile,
    TableReference,
)
from datahub.ingestion.source.unity.report import UnityCatalogReport
from datahub.utilities.lossy_collections import LossyList

logger: logging.Logger = logging.getLogger(__name__)


# TODO: Move to separate proxy/ directory with rest of proxy code
class UnityCatalogProxyProfilingMixin:
    _workspace_client: WorkspaceClient
    _unity_catalog_api: UnityCatalogApi
    report: UnityCatalogReport
    warehouse_id: str

    def check_profiling_connectivity(self):
        self._workspace_client.warehouses.get(self.warehouse_id)
        return True

    def start_warehouse(self) -> Optional[Wait[GetWarehouseResponse]]:
        """Starts a databricks SQL warehouse.

        Returns:
            - A Wait object that can be used to wait for warehouse start completion.
            - None if the warehouse does not exist.
        """
        try:
            return self._workspace_client.warehouses.start(self.warehouse_id)
        except DatabricksError as e:
            logger.warning(f"Unable to start warehouse -- are you sure it exists? {e}")
            return None

    def get_table_stats(
        self,
        ref: TableReference,
        *,
        max_wait_secs: int,
        call_analyze: bool,
        include_columns: bool,
    ) -> Optional[TableProfile]:
        """Returns profiling information for a table.

        Performs three steps:
        1. Call ANALYZE TABLE to compute statistics for all columns
        2. Poll for ANALYZE completion with exponential backoff, with `max_wait_secs` timeout
        3. Get the ANALYZE result via the properties field in the tables API.
            This is supposed to be returned by a DESCRIBE TABLE EXTENDED command, but I don't see it.

        Raises:
            DatabricksError: If any of the above steps fail
        """

        # Currently uses databricks sdk, which is synchronous
        # If we need to improve performance, we can manually make requests via aiohttp
        try:
            if call_analyze:
                response = self._analyze_table(ref, include_columns=include_columns)
                success = self._check_analyze_table_statement_status(
                    response, max_wait_secs=max_wait_secs
                )
                if not success:
                    self.report.profile_table_timeouts.append(str(ref))
                    return None
            return self._get_table_profile(ref, include_columns=include_columns)
        except DatabricksError as e:
            # Attempt to parse out generic part of error message
            msg = str(e)
            idx = (str(msg).find("`") + 1) or (str(msg).find("'") + 1) or len(str(msg))
            base_msg = msg[:idx]
            self.report.profile_table_errors.setdefault(base_msg, LossyList()).append(
                (str(ref), msg)
            )
            logger.warning(
                f"Failure during profiling {ref}, {e.kwargs}: ({e.error_code}) {e}",
                exc_info=True,
            )

            if (
                call_analyze
                and include_columns
                and self._should_retry_unsupported_column(ref, e)
            ):
                return self.get_table_stats(
                    ref,
                    max_wait_secs=max_wait_secs,
                    call_analyze=call_analyze,
                    include_columns=False,
                )
            else:
                return None

    def _should_retry_unsupported_column(
        self, ref: TableReference, e: DatabricksError
    ) -> bool:
        if "[UNSUPPORTED_FEATURE.ANALYZE_UNSUPPORTED_COLUMN_TYPE]" in str(e):
            logger.info(
                f"Attempting to profile table without columns due to unsupported column type: {ref}"
            )
            self.report.num_profile_failed_unsupported_column_type += 1
            return True
        return False

    def _analyze_table(
        self, ref: TableReference, include_columns: bool
    ) -> ExecuteStatementResponse:
        statement = f"ANALYZE TABLE {ref.schema}.{ref.table} COMPUTE STATISTICS"
        if include_columns:
            statement += " FOR ALL COLUMNS"
        response = self._workspace_client.statement_execution.execute_statement(
            statement=statement,
            catalog=ref.catalog,
            wait_timeout="0s",  # Fetch result asynchronously
            warehouse_id=self.warehouse_id,
        )
        self._raise_if_error(response, "analyze-table")
        return response

    def _check_analyze_table_statement_status(
        self, execute_response: ExecuteStatementResponse, max_wait_secs: int
    ) -> bool:
        statement_id: str = execute_response.statement_id
        status: StatementStatus = execute_response.status

        backoff_sec = 1
        total_wait_time = 0
        while (
            total_wait_time < max_wait_secs and status.state != StatementState.SUCCEEDED
        ):
            time.sleep(min(backoff_sec, max_wait_secs - total_wait_time))
            total_wait_time += backoff_sec
            backoff_sec *= 2

            response = self._workspace_client.statement_execution.get_statement(
                statement_id
            )
            self._raise_if_error(response, "get-statement")
            status = response.status

        return status.state == StatementState.SUCCEEDED

    def _get_table_profile(
        self, ref: TableReference, include_columns: bool
    ) -> TableProfile:
        table_info = self._workspace_client.tables.get(ref.qualified_table_name)
        return self._create_table_profile(table_info, include_columns=include_columns)

    def _create_table_profile(
        self, table_info: TableInfo, include_columns: bool
    ) -> TableProfile:
        # Warning: this implementation is brittle -- dependent on properties that can change
        columns_names = [column.name for column in table_info.columns]
        return TableProfile(
            num_rows=self._get_int(table_info, "spark.sql.statistics.numRows"),
            total_size=self._get_int(table_info, "spark.sql.statistics.totalSize"),
            num_columns=len(columns_names),
            column_profiles=[
                self._create_column_profile(column, table_info)
                for column in columns_names
            ]
            if include_columns
            else [],
        )

    def _create_column_profile(
        self, column: str, table_info: TableInfo
    ) -> ColumnProfile:
        return ColumnProfile(
            name=column,
            null_count=self._get_int(
                table_info, f"spark.sql.statistics.colStats.{column}.nullCount"
            ),
            distinct_count=self._get_int(
                table_info, f"spark.sql.statistics.colStats.{column}.distinctCount"
            ),
            min=table_info.properties.get(
                f"spark.sql.statistics.colStats.{column}.min"
            ),
            max=table_info.properties.get(
                f"spark.sql.statistics.colStats.{column}.max"
            ),
            avg_len=table_info.properties.get(
                f"spark.sql.statistics.colStats.{column}.avgLen"
            ),
            max_len=table_info.properties.get(
                f"spark.sql.statistics.colStats.{column}.maxLen"
            ),
            version=table_info.properties.get(
                f"spark.sql.statistics.colStats.{column}.version"
            ),
        )

    def _get_int(self, table_info: TableInfo, field: str) -> Optional[int]:
        value = table_info.properties.get(field)
        if value is not None:
            try:
                return int(value)
            except ValueError:
                logger.warning(
                    f"Failed to parse int for {table_info.name} - {field}: {value}"
                )
                self.report.num_profile_failed_int_casts += 1
        return None

    @staticmethod
    def _raise_if_error(
        response: Union[ExecuteStatementResponse, GetStatementResponse], key: str
    ) -> None:
        if response.status.state in [
            StatementState.FAILED,
            StatementState.CANCELED,
            StatementState.CLOSED,
        ]:
            raise DatabricksError(
                response.status.error.message,
                error_code=response.status.error.error_code.value,
                status=response.status.state.value,
                context=key,
            )

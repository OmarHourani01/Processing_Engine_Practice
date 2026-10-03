from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = "page_size"
    max_page_size = 100

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.page.paginator.count,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "results": data,
            }
        )


class JobPagination(StandardPagination):
    """Return every task for one expanded batch in a single request."""

    batch_page_size = 2000  # Two image tasks for each of at most 1,000 files.

    def get_page_size(self, request):
        if request.query_params.get("parent_job"):
            return self.batch_page_size
        return super().get_page_size(request)

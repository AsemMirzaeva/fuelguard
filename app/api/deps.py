from typing import Annotated

from fastapi import Depends, Query


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        self.limit = limit
        self.offset = offset


PageParams = Annotated[Pagination, Depends()]

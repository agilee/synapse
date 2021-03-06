# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
from typing import Iterable, Iterator, List, Tuple

from canonicaljson import encode_canonical_json

from twisted.internet import defer

from synapse.storage._base import SQLBaseStore, db_to_json
from synapse.util.caches.descriptors import cachedInlineCallbacks, cachedList

logger = logging.getLogger(__name__)


class PusherWorkerStore(SQLBaseStore):
    def _decode_pushers_rows(self, rows: Iterable[dict]) -> Iterator[dict]:
        """JSON-decode the data in the rows returned from the `pushers` table

        Drops any rows whose data cannot be decoded
        """
        for r in rows:
            dataJson = r["data"]
            try:
                r["data"] = db_to_json(dataJson)
            except Exception as e:
                logger.warning(
                    "Invalid JSON in data for pusher %d: %s, %s",
                    r["id"],
                    dataJson,
                    e.args[0],
                )
                continue

            yield r

    @defer.inlineCallbacks
    def user_has_pusher(self, user_id):
        ret = yield self.db.simple_select_one_onecol(
            "pushers", {"user_name": user_id}, "id", allow_none=True
        )
        return ret is not None

    def get_pushers_by_app_id_and_pushkey(self, app_id, pushkey):
        return self.get_pushers_by({"app_id": app_id, "pushkey": pushkey})

    def get_pushers_by_user_id(self, user_id):
        return self.get_pushers_by({"user_name": user_id})

    @defer.inlineCallbacks
    def get_pushers_by(self, keyvalues):
        ret = yield self.db.simple_select_list(
            "pushers",
            keyvalues,
            [
                "id",
                "user_name",
                "access_token",
                "profile_tag",
                "kind",
                "app_id",
                "app_display_name",
                "device_display_name",
                "pushkey",
                "ts",
                "lang",
                "data",
                "last_stream_ordering",
                "last_success",
                "failing_since",
            ],
            desc="get_pushers_by",
        )
        return self._decode_pushers_rows(ret)

    @defer.inlineCallbacks
    def get_all_pushers(self):
        def get_pushers(txn):
            txn.execute("SELECT * FROM pushers")
            rows = self.db.cursor_to_dict(txn)

            return self._decode_pushers_rows(rows)

        rows = yield self.db.runInteraction("get_all_pushers", get_pushers)
        return rows

    async def get_all_updated_pushers_rows(
        self, instance_name: str, last_id: int, current_id: int, limit: int
    ) -> Tuple[List[Tuple[int, tuple]], int, bool]:
        """Get updates for pushers replication stream.

        Args:
            instance_name: The writer we want to fetch updates from. Unused
                here since there is only ever one writer.
            last_id: The token to fetch updates from. Exclusive.
            current_id: The token to fetch updates up to. Inclusive.
            limit: The requested limit for the number of rows to return. The
                function may return more or fewer rows.

        Returns:
            A tuple consisting of: the updates, a token to use to fetch
            subsequent updates, and whether we returned fewer rows than exists
            between the requested tokens due to the limit.

            The token returned can be used in a subsequent call to this
            function to get further updatees.

            The updates are a list of 2-tuples of stream ID and the row data
        """

        if last_id == current_id:
            return [], current_id, False

        def get_all_updated_pushers_rows_txn(txn):
            sql = """
                SELECT id, user_name, app_id, pushkey
                FROM pushers
                WHERE ? < id AND id <= ?
                ORDER BY id ASC LIMIT ?
            """
            txn.execute(sql, (last_id, current_id, limit))
            updates = [
                (stream_id, (user_name, app_id, pushkey, False))
                for stream_id, user_name, app_id, pushkey in txn
            ]

            sql = """
                SELECT stream_id, user_id, app_id, pushkey
                FROM deleted_pushers
                WHERE ? < stream_id AND stream_id <= ?
                ORDER BY stream_id ASC LIMIT ?
            """
            txn.execute(sql, (last_id, current_id, limit))
            updates.extend(
                (stream_id, (user_name, app_id, pushkey, True))
                for stream_id, user_name, app_id, pushkey in txn
            )

            updates.sort()  # Sort so that they're ordered by stream id

            limited = False
            upper_bound = current_id
            if len(updates) >= limit:
                limited = True
                upper_bound = updates[-1][0]

            return updates, upper_bound, limited

        return await self.db.runInteraction(
            "get_all_updated_pushers_rows", get_all_updated_pushers_rows_txn
        )

    @cachedInlineCallbacks(num_args=1, max_entries=15000)
    def get_if_user_has_pusher(self, user_id):
        # This only exists for the cachedList decorator
        raise NotImplementedError()

    @cachedList(
        cached_method_name="get_if_user_has_pusher",
        list_name="user_ids",
        num_args=1,
        inlineCallbacks=True,
    )
    def get_if_users_have_pushers(self, user_ids):
        rows = yield self.db.simple_select_many_batch(
            table="pushers",
            column="user_name",
            iterable=user_ids,
            retcols=["user_name"],
            desc="get_if_users_have_pushers",
        )

        result = {user_id: False for user_id in user_ids}
        result.update({r["user_name"]: True for r in rows})

        return result

    @defer.inlineCallbacks
    def update_pusher_last_stream_ordering(
        self, app_id, pushkey, user_id, last_stream_ordering
    ):
        yield self.db.simple_update_one(
            "pushers",
            {"app_id": app_id, "pushkey": pushkey, "user_name": user_id},
            {"last_stream_ordering": last_stream_ordering},
            desc="update_pusher_last_stream_ordering",
        )

    @defer.inlineCallbacks
    def update_pusher_last_stream_ordering_and_success(
        self, app_id, pushkey, user_id, last_stream_ordering, last_success
    ):
        """Update the last stream ordering position we've processed up to for
        the given pusher.

        Args:
            app_id (str)
            pushkey (str)
            last_stream_ordering (int)
            last_success (int)

        Returns:
            Deferred[bool]: True if the pusher still exists; False if it has been deleted.
        """
        updated = yield self.db.simple_update(
            table="pushers",
            keyvalues={"app_id": app_id, "pushkey": pushkey, "user_name": user_id},
            updatevalues={
                "last_stream_ordering": last_stream_ordering,
                "last_success": last_success,
            },
            desc="update_pusher_last_stream_ordering_and_success",
        )

        return bool(updated)

    @defer.inlineCallbacks
    def update_pusher_failing_since(self, app_id, pushkey, user_id, failing_since):
        yield self.db.simple_update(
            table="pushers",
            keyvalues={"app_id": app_id, "pushkey": pushkey, "user_name": user_id},
            updatevalues={"failing_since": failing_since},
            desc="update_pusher_failing_since",
        )

    @defer.inlineCallbacks
    def get_throttle_params_by_room(self, pusher_id):
        res = yield self.db.simple_select_list(
            "pusher_throttle",
            {"pusher": pusher_id},
            ["room_id", "last_sent_ts", "throttle_ms"],
            desc="get_throttle_params_by_room",
        )

        params_by_room = {}
        for row in res:
            params_by_room[row["room_id"]] = {
                "last_sent_ts": row["last_sent_ts"],
                "throttle_ms": row["throttle_ms"],
            }

        return params_by_room

    @defer.inlineCallbacks
    def set_throttle_params(self, pusher_id, room_id, params):
        # no need to lock because `pusher_throttle` has a primary key on
        # (pusher, room_id) so simple_upsert will retry
        yield self.db.simple_upsert(
            "pusher_throttle",
            {"pusher": pusher_id, "room_id": room_id},
            params,
            desc="set_throttle_params",
            lock=False,
        )


class PusherStore(PusherWorkerStore):
    def get_pushers_stream_token(self):
        return self._pushers_id_gen.get_current_token()

    @defer.inlineCallbacks
    def add_pusher(
        self,
        user_id,
        access_token,
        kind,
        app_id,
        app_display_name,
        device_display_name,
        pushkey,
        pushkey_ts,
        lang,
        data,
        last_stream_ordering,
        profile_tag="",
    ):
        with self._pushers_id_gen.get_next() as stream_id:
            # no need to lock because `pushers` has a unique key on
            # (app_id, pushkey, user_name) so simple_upsert will retry
            yield self.db.simple_upsert(
                table="pushers",
                keyvalues={"app_id": app_id, "pushkey": pushkey, "user_name": user_id},
                values={
                    "access_token": access_token,
                    "kind": kind,
                    "app_display_name": app_display_name,
                    "device_display_name": device_display_name,
                    "ts": pushkey_ts,
                    "lang": lang,
                    "data": bytearray(encode_canonical_json(data)),
                    "last_stream_ordering": last_stream_ordering,
                    "profile_tag": profile_tag,
                    "id": stream_id,
                },
                desc="add_pusher",
                lock=False,
            )

            user_has_pusher = self.get_if_user_has_pusher.cache.get(
                (user_id,), None, update_metrics=False
            )

            if user_has_pusher is not True:
                # invalidate, since we the user might not have had a pusher before
                yield self.db.runInteraction(
                    "add_pusher",
                    self._invalidate_cache_and_stream,
                    self.get_if_user_has_pusher,
                    (user_id,),
                )

    @defer.inlineCallbacks
    def delete_pusher_by_app_id_pushkey_user_id(self, app_id, pushkey, user_id):
        def delete_pusher_txn(txn, stream_id):
            self._invalidate_cache_and_stream(
                txn, self.get_if_user_has_pusher, (user_id,)
            )

            self.db.simple_delete_one_txn(
                txn,
                "pushers",
                {"app_id": app_id, "pushkey": pushkey, "user_name": user_id},
            )

            # it's possible for us to end up with duplicate rows for
            # (app_id, pushkey, user_id) at different stream_ids, but that
            # doesn't really matter.
            self.db.simple_insert_txn(
                txn,
                table="deleted_pushers",
                values={
                    "stream_id": stream_id,
                    "app_id": app_id,
                    "pushkey": pushkey,
                    "user_id": user_id,
                },
            )

        with self._pushers_id_gen.get_next() as stream_id:
            yield self.db.runInteraction("delete_pusher", delete_pusher_txn, stream_id)

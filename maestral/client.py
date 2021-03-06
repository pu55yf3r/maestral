# -*- coding: utf-8 -*-
"""
@author: Sam Schott  (ss2151@cam.ac.uk)

(c) Sam Schott; This work is licensed under the MIT licence.

This modules contains the Dropbox API client. It wraps calls to the Dropbox Python SDK
and handles exceptions, chunked uploads or downloads, etc.

"""

# system imports
import errno
import os
import os.path as osp
import time
import logging
import functools
import contextlib
from datetime import datetime, timezone
from typing import (
    Callable,
    Union,
    Any,
    Type,
    Tuple,
    List,
    TypeVar,
    Optional,
    TYPE_CHECKING,
)

# external imports
import requests
from dropbox import (  # type: ignore
    Dropbox,
    dropbox,
    files,
    users,
    exceptions,
    async_,
    auth,
    oauth,
)

# local imports
from maestral import __version__
from maestral.oauth import OAuth2Session
from maestral.errors import (
    MaestralApiError,
    SyncError,
    InsufficientPermissionsError,
    PathError,
    FileReadError,
    InsufficientSpaceError,
    FileConflictError,
    FolderConflictError,
    ConflictError,
    UnsupportedFileError,
    RestrictedContentError,
    NotFoundError,
    NotAFolderError,
    IsAFolderError,
    FileSizeError,
    OutOfMemoryError,
    BadInputError,
    DropboxAuthError,
    TokenExpiredError,
    TokenRevokedError,
    CursorResetError,
    DropboxServerError,
    NoDropboxDirError,
    InotifyError,
    NotLinkedError,
    InvalidDbidError,
)
from maestral.config import MaestralState
from maestral.constants import DROPBOX_APP_KEY
from maestral.utils import natural_size, chunks, clamp

if TYPE_CHECKING:
    from maestral.sync import SyncEvent

logger = logging.getLogger(__name__)

# type definitions
LocalError = Union[MaestralApiError, OSError]
WriteErrorType = Type[
    Union[
        SyncError,
        InsufficientPermissionsError,
        PathError,
        InsufficientSpaceError,
        FileConflictError,
        FolderConflictError,
        ConflictError,
        FileReadError,
    ]
]
LookupErrorType = Type[
    Union[
        SyncError,
        UnsupportedFileError,
        RestrictedContentError,
        NotFoundError,
        NotAFolderError,
        IsAFolderError,
        PathError,
    ]
]
SessionLookupErrorType = Type[
    Union[
        SyncError,
        FileSizeError,
    ]
]
_FT = Callable[..., Any]
_T = TypeVar("_T")


# create single requests session for all clients
SESSION = dropbox.create_session()
_major_minor_version = ".".join(__version__.split(".")[:2])
USER_AGENT = f"Maestral/v{_major_minor_version}"


CONNECTION_ERRORS = (
    requests.exceptions.Timeout,
    requests.exceptions.RetryError,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    ConnectionError,
)


class SpaceUsage(users.SpaceUsage):
    @property
    def allocation_type(self) -> str:
        if self.allocation.is_team():
            return "team"
        elif self.allocation.is_individual():
            return "individual"
        else:
            return ""

    def __str__(self) -> str:

        if self.allocation.is_individual():
            used = self.used
            allocated = self.allocation.get_individual().allocated
        elif self.allocation.is_team():
            used = self.allocation.get_team().used
            allocated = self.allocation.get_team().allocated
        else:
            return natural_size(self.used)

        percent = used / allocated
        return f"{percent:.1%} of {natural_size(allocated)} used"

    @classmethod
    def from_dbx_space_usage(cls, su: users.SpaceUsage) -> "SpaceUsage":
        return cls(used=su.used, allocation=su.allocation)


def to_maestral_error(
    dbx_path_arg: Optional[int] = None, local_path_arg: Optional[int] = None
) -> Callable[[_FT], _FT]:
    """
    Returns a decorator that converts instances of :class:`OSError` and
    :class:`exceptions.DropboxException` to :class:`errors.MaestralApiError`.

    :param dbx_path_arg: Argument number to take as dbx_path for exception.
    :param local_path_arg: Argument number to take as local_path_arg for exception.
    """

    def decorator(func: _FT) -> _FT:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:

            dbx_path = args[dbx_path_arg] if dbx_path_arg else None
            local_path = args[local_path_arg] if local_path_arg else None

            try:
                return func(*args, **kwargs)
            except exceptions.DropboxException as exc:
                raise dropbox_to_maestral_error(exc, dbx_path, local_path)
            # catch connection errors first, they may inherit from OSError
            except CONNECTION_ERRORS:
                raise ConnectionError("Cannot connect to Dropbox")
            except OSError as exc:
                raise os_to_maestral_error(exc, dbx_path, local_path)

        return wrapper

    return decorator


class DropboxClient:
    """Client for the Dropbox SDK.

    This client defines basic methods to wrap Dropbox Python SDK calls, such as
    creating, moving, modifying and deleting files and folders on Dropbox and
    downloading files from Dropbox.

    All Dropbox SDK exceptions and :class:`OSError` instances if related to accessing or
    saving local files will be caught and reraised as a
    :class:`errors.MaestralApiError`. Connection errors from requests will be caught and
    reraised as :class:`ConnectionError`.

    :param config_name: Name of config file and state file to use.
    :param timeout: Timeout for individual requests. Defaults to 100 sec if not given.
    """

    SDK_VERSION: str = "2.0"

    _dbx: Optional[Dropbox]

    def __init__(self, config_name: str, timeout: float = 100) -> None:

        self.config_name = config_name
        self.auth = OAuth2Session(config_name)

        self._timeout = timeout
        self._backoff_until = 0
        self._dbx = None
        self._state = MaestralState(config_name)

    # ---- linking API -----------------------------------------------------------------

    @property
    def dbx(self) -> Dropbox:
        """The actual Python Dropbox SDK"""
        if not self.linked:
            raise NotLinkedError(
                "No auth token set", "Please link a Dropbox account first."
            )

        return self._dbx

    @property
    def linked(self) -> bool:
        """
        Indicates if the client is linked to a Dropbox account (read only). This will
        block until the user's keyring is unlocked to load the saved auth token.

        :raises: :class:`errors.KeyringAccessError` if keyring access fails.
        """

        if self._dbx:
            return True

        elif self.auth.linked:  # this will trigger keyring access on first call

            if self.auth.token_access_type == "legacy":
                self._init_sdk_with_token(access_token=self.auth.access_token)
            else:
                self._init_sdk_with_token(refresh_token=self.auth.refresh_token)

            return True

        else:
            return False

    def get_auth_url(self) -> str:
        """
        Returns a URL to authorize access to a Dropbox account. To link a Dropbox
        account, retrieve an auth token from the URL and link Maestral by calling
        :meth:`link` with the provided token.

        :returns: URL to retrieve an OAuth token.
        """
        return self.auth.get_auth_url()

    def link(self, token: str) -> int:
        """
        Links Maestral with a Dropbox account using the given access token. The token
        will be stored for future usage as documented in the :mod:`oauth` module.

        :param token: OAuth token for Dropbox access.
        :returns: 0 on success, 1 for an invalid token and 2 for connection errors.
        """

        res = self.auth.verify_auth_token(token)

        if res == self.auth.Success:
            self.auth.save_creds()

            self._init_sdk_with_token(
                refresh_token=self.auth.refresh_token,
                access_token=self.auth.access_token,
                access_token_expiration=self.auth.access_token_expiration,
            )

            try:
                self.get_account_info()
                self.get_space_usage()
            except ConnectionError:
                pass

        return res

    @to_maestral_error()
    def unlink(self) -> None:
        """
        Unlinks the Dropbox account.

        :raises: :class:`errors.KeyringAccessError`
        :raises: :class:`errors.DropboxAuthError`
        """
        self.auth.delete_creds()
        self.dbx.auth_token_revoke()  # should only raise auth errors

    def _init_sdk_with_token(
        self,
        refresh_token: Optional[str] = None,
        access_token: Optional[str] = None,
        access_token_expiration: Optional[datetime] = None,
    ) -> None:
        """
        Sets the access tokens for the Dropbox API. This will create a new SDK instance
        with new tokens.

        :param refresh_token: Long-lived refresh token to generate new access tokens.
        :param access_token: Short-lived auth token.
        :param access_token_expiration: Expiry time of auth token.
        """

        if refresh_token or access_token:

            self._dbx = Dropbox(
                oauth2_refresh_token=refresh_token,
                oauth2_access_token=access_token,
                oauth2_access_token_expiration=access_token_expiration,
                app_key=DROPBOX_APP_KEY,
                session=SESSION,
                user_agent=USER_AGENT,
                timeout=self._timeout,
            )
        else:
            self._dbx = None

    @property
    def account_id(self) -> Optional[str]:
        """The unique Dropbox ID of the linked account"""
        return self.auth.account_id

    # ---- SDK wrappers ----------------------------------------------------------------

    @to_maestral_error()
    def get_account_info(self, dbid: Optional[str] = None) -> users.FullAccount:
        """
        Gets current account information.

        :param dbid: Dropbox ID of account. If not given, will get the info of the
            currently linked account.
        :returns: Account info.
        """
        if dbid:
            res = self.dbx.users_get_account(dbid)
        else:
            res = self.dbx.users_get_current_account()

        if not dbid:
            # save our own account info to config
            if res.account_type.is_basic():
                account_type = "basic"
            elif res.account_type.is_business():
                account_type = "business"
            elif res.account_type.is_pro():
                account_type = "pro"
            else:
                account_type = ""

            self._state.set("account", "email", res.email)
            self._state.set("account", "display_name", res.name.display_name)
            self._state.set("account", "abbreviated_name", res.name.abbreviated_name)
            self._state.set("account", "type", account_type)

        return res

    @to_maestral_error()
    def get_space_usage(self) -> SpaceUsage:
        """
        :returns: The space usage of the currently linked account.
        """
        res = self.dbx.users_get_space_usage()

        # convert from users.SpaceUsage to SpaceUsage
        space_usage = SpaceUsage.from_dbx_space_usage(res)

        # save results to config
        self._state.set("account", "usage", str(space_usage))
        self._state.set("account", "usage_type", space_usage.allocation_type)

        return space_usage

    @to_maestral_error(dbx_path_arg=1)
    def get_metadata(self, dbx_path: str, **kwargs) -> files.Metadata:
        """
        Gets metadata for an item on Dropbox or returns ``False`` if no metadata is
        available. Keyword arguments are passed on to Dropbox SDK files_get_metadata
        call.

        :param dbx_path: Path of folder on Dropbox.
        :param kwargs: Keyword arguments for Dropbox SDK files_download_to_file.
        :returns: Metadata of item at the given path or ``None``.
        """

        try:
            return self.dbx.files_get_metadata(dbx_path, **kwargs)
        except exceptions.ApiError:
            # DropboxAPI error is only raised when the item does not exist on Dropbox
            # this is handled on a DEBUG level since we use call `get_metadata` to check
            # if a file exists
            pass

    @to_maestral_error(dbx_path_arg=1)
    def list_revisions(
        self, dbx_path: str, mode: str = "path", limit: int = 10
    ) -> files.ListRevisionsResult:
        """
        Lists all file revisions for the given file.

        :param dbx_path: Path to file on Dropbox.
        :param mode: Must be 'path' or 'id'. If 'id', specify the Dropbox file ID
            instead of the file path to get revisions across move and rename events.
        :param limit: Maximum number of revisions to list. Defaults to 10.
        :returns: File revision history.
        """

        mode = files.ListRevisionsMode(mode)
        return self.dbx.files_list_revisions(dbx_path, mode=mode, limit=limit)

    @to_maestral_error(dbx_path_arg=1)
    def restore(self, dbx_path: str, rev: str) -> files.FileMetadata:
        """
        Restore an old revision of a file.

        :param dbx_path: The path to save the restored file.
        :param rev: The revision to restore. Old revisions can be listed with
            :meth:`list_revisions`.
        :returns: Metadata of restored file.
        """

        return self.dbx.files_restore(dbx_path, rev)

    @to_maestral_error(dbx_path_arg=1)
    def download(
        self,
        dbx_path: str,
        local_path: str,
        sync_event: Optional["SyncEvent"] = None,
        **kwargs,
    ) -> files.FileMetadata:
        """
        Downloads file from Dropbox to our local folder.

        :param dbx_path: Path to file on Dropbox or rev number.
        :param local_path: Path to local download destination.
        :param sync_event: If given, the sync event will be updated with the number of
            downloaded bytes.
        :param kwargs: Keyword arguments for Dropbox SDK files_download_to_file.
        :returns: Metadata of downloaded item.
        """
        # create local directory if not present
        dst_path_directory = osp.dirname(local_path)
        try:
            os.makedirs(dst_path_directory)
        except FileExistsError:
            pass

        md, http_resp = self.dbx.files_download(dbx_path, **kwargs)

        chunksize = 2 ** 16
        size_str = natural_size(md.size)

        with open(local_path, "wb") as f:
            with contextlib.closing(http_resp):
                for c in http_resp.iter_content(chunksize):
                    f.write(c)
                    downloaded = f.tell()
                    logger.debug(
                        "Downloading %s: %s/%s",
                        dbx_path,
                        natural_size(downloaded),
                        size_str,
                    )
                    if sync_event:
                        sync_event.completed = downloaded

        # dropbox SDK provides naive datetime in UTC
        client_mod_timestamp = md.client_modified.replace(
            tzinfo=timezone.utc
        ).timestamp()
        server_mod_timestamp = md.server_modified.replace(
            tzinfo=timezone.utc
        ).timestamp()

        # enforce client_modified < server_modified
        timestamp = min(client_mod_timestamp, server_mod_timestamp, time.time())
        # set mtime of downloaded file
        os.utime(local_path, (time.time(), timestamp))

        return md

    @to_maestral_error(local_path_arg=1, dbx_path_arg=2)
    def upload(
        self,
        local_path: str,
        dbx_path: str,
        chunk_size: int = 5 * 10 ** 6,
        sync_event: Optional["SyncEvent"] = None,
        **kwargs,
    ) -> files.FileMetadata:
        """
        Uploads local file to Dropbox.

        :param local_path: Path of local file to upload.
        :param dbx_path: Path to save file on Dropbox.
        :param kwargs: Keyword arguments for Dropbox SDK files_upload.
        :param chunk_size: Maximum size for individual uploads. If larger than 150 MB,
            it will be set to 150 MB.
        :param sync_event: If given, the sync event will be updated with the number of
            downloaded bytes.
        :returns: Metadata of uploaded file.
        """

        chunk_size = clamp(chunk_size, 10 ** 5, 150 * 10 ** 6)

        size = osp.getsize(local_path)
        size_str = natural_size(size)

        # dropbox SDK takes naive datetime in UTC
        mtime = osp.getmtime(local_path)
        mtime_dt = datetime.utcfromtimestamp(mtime)

        if size <= chunk_size:
            with open(local_path, "rb") as f:
                md = self.dbx.files_upload(
                    f.read(), dbx_path, client_modified=mtime_dt, **kwargs
                )
                if sync_event:
                    sync_event.completed = f.tell()
            return md
        else:
            # Note: We currently do not support resuming interrupted uploads. Dropbox
            # keeps upload sessions open for 48h so this could be done in the future.
            with open(local_path, "rb") as f:
                session_start = self.dbx.files_upload_session_start(f.read(chunk_size))
                uploaded = f.tell()

                cursor = files.UploadSessionCursor(
                    session_id=session_start.session_id, offset=uploaded
                )
                commit = files.CommitInfo(
                    path=dbx_path, client_modified=mtime_dt, **kwargs
                )

                if sync_event:
                    sync_event.completed = uploaded

                while True:
                    try:
                        if size - f.tell() <= chunk_size:
                            md = self.dbx.files_upload_session_finish(
                                f.read(chunk_size), cursor, commit
                            )

                        else:
                            self.dbx.files_upload_session_append_v2(
                                f.read(chunk_size), cursor
                            )
                            md = None

                        # housekeeping
                        uploaded = f.tell()
                        logger.debug(
                            "Uploading %s: %s/%s",
                            dbx_path,
                            natural_size(uploaded),
                            size_str,
                        )
                        if sync_event:
                            sync_event.completed = uploaded

                        if md:
                            return md
                        else:
                            cursor.offset = uploaded

                    except exceptions.DropboxException as exc:
                        error = getattr(exc, "error", None)
                        if (
                            isinstance(error, files.UploadSessionFinishError)
                            and error.is_lookup_failed()
                        ):
                            session_lookup_error = error.get_lookup_failed()
                        elif isinstance(error, files.UploadSessionLookupError):
                            session_lookup_error = error
                        else:
                            raise exc

                        if session_lookup_error.is_incorrect_offset():
                            o = (
                                session_lookup_error.get_incorrect_offset().correct_offset
                            )
                            # reset position in file
                            f.seek(o)
                            cursor.offset = f.tell()
                        else:
                            raise exc

    @to_maestral_error(dbx_path_arg=1)
    def remove(self, dbx_path: str, **kwargs) -> files.Metadata:
        """
        Removes a file / folder from Dropbox.

        :param dbx_path: Path to file on Dropbox.
        :param kwargs: Keyword arguments for Dropbox SDK files_delete_v2.
        :returns: Metadata of deleted item.
        """
        # try to remove file (response will be metadata, probably)
        res = self.dbx.files_delete_v2(dbx_path, **kwargs)
        md = res.metadata

        return md

    @to_maestral_error()
    def remove_batch(
        self, entries: List[Tuple[str, str]], batch_size: int = 900
    ) -> List[Union[files.Metadata, MaestralApiError]]:
        """
        Delete multiple items on Dropbox in a batch job.

        :param entries: List of Dropbox paths and "rev"s to delete. If a "rev" is not
            None, the file will only be deleted if it matches the rev on Dropbox. This
            is not supported when deleting a folder.
        :param batch_size: Number of items to delete in each batch. Dropbox allows
            batches of up to 1,000 items. Larger values will be capped automatically.
        :returns: List of Metadata for deleted items or :class:`errors.SyncError` for
            failures. Results will be in the same order as the original input.
        """

        batch_size = clamp(batch_size, 1, 1000)

        res_entries = []
        result_list = []

        # up two ~ 1,000 entries allowed per batch according to
        # https://www.dropbox.com/developers/reference/data-ingress-guide
        for chunk in chunks(entries, n=batch_size):

            arg = [files.DeleteArg(e[0], e[1]) for e in chunk]
            res = self.dbx.files_delete_batch(arg)

            if res.is_complete():
                batch_res = res.get_complete()
                res_entries.extend(batch_res.entries)

            elif res.is_async_job_id():
                async_job_id = res.get_async_job_id()

                time.sleep(1.0)
                res = self.dbx.files_delete_batch_check(async_job_id)

                check_interval = round(len(chunk) / 100, 1)

                while res.is_in_progress():
                    time.sleep(check_interval)
                    res = self.dbx.files_delete_batch_check(async_job_id)

                if res.is_complete():
                    batch_res = res.get_complete()
                    res_entries.extend(batch_res.entries)

                elif res.is_failed():
                    error = res.get_failed()
                    if error.is_too_many_write_operations():
                        title = "Could not delete items"
                        text = (
                            "There are too many write operations happening in your "
                            "Dropbox. Please try again later."
                        )
                        raise SyncError(title, text)

        for i, entry in enumerate(res_entries):
            if entry.is_success():
                result_list.append(entry.get_success().metadata)
            elif entry.is_failure():
                exc = exceptions.ApiError(
                    error=entry.get_failure(),
                    user_message_text="",
                    user_message_locale="",
                    request_id="",
                )
                sync_err = dropbox_to_maestral_error(exc, dbx_path=entries[i][0])
                result_list.append(sync_err)

        return result_list

    @to_maestral_error(dbx_path_arg=2)
    def move(self, dbx_path: str, new_path: str, **kwargs) -> files.Metadata:
        """
        Moves / renames files or folders on Dropbox.

        :param dbx_path: Path to file/folder on Dropbox.
        :param new_path: New path on Dropbox to move to.
        :param kwargs: Keyword arguments for Dropbox SDK files_move_v2.
        :returns: Metadata of moved item.
        """
        res = self.dbx.files_move_v2(
            dbx_path,
            new_path,
            allow_shared_folder=True,
            allow_ownership_transfer=True,
            **kwargs,
        )
        md = res.metadata

        return md

    @to_maestral_error(dbx_path_arg=1)
    def make_dir(self, dbx_path: str, **kwargs) -> files.FolderMetadata:
        """
        Creates a folder on Dropbox.

        :param dbx_path: Path of Dropbox folder.
        :param kwargs: Keyword arguments for Dropbox SDK files_create_folder_v2.
        :returns: Metadata of created folder.
        """
        res = self.dbx.files_create_folder_v2(dbx_path, **kwargs)
        md = res.metadata

        return md

    @to_maestral_error()
    def make_dir_batch(
        self, dbx_paths: List[str], batch_size: int = 900, **kwargs
    ) -> List[Union[files.Metadata, MaestralApiError]]:
        """
        Creates multiple folders on Dropbox in a batch job.

        :param dbx_paths: List of dropbox folder paths.
        :param batch_size: Number of folders to create in each batch. Dropbox allows
            batches of up to 1,000 folders. Larger values will be capped automatically.
        :param kwargs: Keyword arguments for Dropbox SDK files_create_folder_batch.
        :returns: List of Metadata for created folders or SyncError for failures.
            Entries will be in the same order as given paths.
        """
        batch_size = clamp(batch_size, 1, 1000)

        entries = []
        result_list = []

        # up two ~ 1,000 entries allowed per batch according to
        # https://www.dropbox.com/developers/reference/data-ingress-guide
        for chunk in chunks(dbx_paths, n=batch_size):
            res = self.dbx.files_create_folder_batch(chunk, **kwargs)
            if res.is_complete():
                batch_res = res.get_complete()
                entries.extend(batch_res.entries)
            elif res.is_async_job_id():
                async_job_id = res.get_async_job_id()

                time.sleep(1.0)
                res = self.dbx.files_create_folder_batch_check(async_job_id)

                check_interval = round(len(chunk) / 100, 1)

                while res.is_in_progress():
                    time.sleep(check_interval)
                    res = self.dbx.files_create_folder_batch_check(async_job_id)

                if res.is_complete():
                    batch_res = res.get_complete()
                    entries.extend(batch_res.entries)

                elif res.is_failed():
                    error = res.get_failed()
                    if error.is_too_many_files():
                        res_list = self.make_dir_batch(
                            chunk, batch_size=round(batch_size / 2), **kwargs
                        )
                        result_list.extend(res_list)

        for i, entry in enumerate(entries):
            if entry.is_success():
                result_list.append(entry.get_success().metadata)
            elif entry.is_failure():
                exc = exceptions.ApiError(
                    error=entry.get_failure(),
                    user_message_text="",
                    user_message_locale="",
                    request_id="",
                )
                sync_err = dropbox_to_maestral_error(exc, dbx_path=dbx_paths[i])
                result_list.append(sync_err)

        return result_list

    @to_maestral_error(dbx_path_arg=1)
    def get_latest_cursor(
        self, dbx_path: str, include_non_downloadable_files: bool = False, **kwargs
    ) -> str:
        """
        Gets the latest cursor for the given folder and subfolders.

        :param dbx_path: Path of folder on Dropbox.
        :param include_non_downloadable_files: If ``True``, files that cannot be
            downloaded (at the moment only G-suite files on Dropbox) will be included.
        :param kwargs: Other keyword arguments for Dropbox SDK files_list_folder.
        :returns: The latest cursor representing a state of a folder and its subfolders.
        """

        dbx_path = "" if dbx_path == "/" else dbx_path

        res = self.dbx.files_list_folder_get_latest_cursor(
            dbx_path,
            include_non_downloadable_files=include_non_downloadable_files,
            recursive=True,
            **kwargs,
        )

        return res.cursor

    @to_maestral_error(dbx_path_arg=1)
    def list_folder(
        self,
        dbx_path: str,
        max_retries_on_timeout: int = 4,
        include_non_downloadable_files: bool = False,
        **kwargs,
    ) -> files.ListFolderResult:
        """
        Lists the contents of a folder on Dropbox.

        :param dbx_path: Path of folder on Dropbox.
        :param max_retries_on_timeout: Number of times to try again if Dropbox servers
            do not respond within the timeout. Occasional timeouts may occur for very
            large Dropbox folders.
        :param include_non_downloadable_files: If ``True``, files that cannot be
            downloaded (at the moment only G-suite files on Dropbox) will be included.
        :param kwargs: Other keyword arguments for Dropbox SDK files_list_folder.
        :returns: Content of given folder.
        """

        dbx_path = "" if dbx_path == "/" else dbx_path

        results = []

        res = self.dbx.files_list_folder(
            dbx_path,
            include_non_downloadable_files=include_non_downloadable_files,
            **kwargs,
        )
        results.append(res)

        idx = 0

        while results[-1].has_more:

            idx += len(results[-1].entries)
            logger.info(f"Indexing {idx}...")

            attempt = 0

            while True:
                try:
                    more_results = self.dbx.files_list_folder_continue(
                        results[-1].cursor
                    )
                    results.append(more_results)
                    break
                except requests.exceptions.ReadTimeout:
                    attempt += 1
                    if attempt <= max_retries_on_timeout:
                        time.sleep(5.0)
                    else:
                        raise

        return self.flatten_results(results)

    @staticmethod
    def flatten_results(
        results: List[files.ListFolderResult],
    ) -> files.ListFolderResult:
        """
        Flattens a list of :class:`files.ListFolderResult` instances to a single
        instance with the cursor of the last entry in the list.

        :param results: List of :class:`files.ListFolderResult` instances.
        :returns: Flattened list folder result.
        """
        entries_all = []
        for result in results:
            entries_all += result.entries

        results_flattened = files.ListFolderResult(
            entries=entries_all, cursor=results[-1].cursor, has_more=False
        )

        return results_flattened

    @to_maestral_error()
    def wait_for_remote_changes(self, last_cursor: str, timeout: int = 40) -> bool:
        """
        Waits for remote changes since ``last_cursor``. Call this method after
        starting the Dropbox client and periodically to get the latest updates.

        :param last_cursor: Last to cursor to compare for changes.
        :param timeout: Seconds to wait until timeout. Must be between 30 and 480.
        :returns: ``True`` if changes are available, ``False`` otherwise.
        """

        if not 30 <= timeout <= 480:
            raise ValueError("Timeout must be in range [30, 480]")

        # honour last request to back off
        time_to_backoff = max(self._backoff_until - time.time(), 0)
        time.sleep(time_to_backoff)

        result = self.dbx.files_list_folder_longpoll(last_cursor, timeout=timeout)

        # keep track of last longpoll, back off if requested by SDK
        if result.backoff:
            self._backoff_until = time.time() + result.backoff + 5.0
        else:
            self._backoff_until = 0

        return result.changes

    @to_maestral_error()
    def list_remote_changes(self, last_cursor: str) -> files.ListFolderResult:
        """
        Lists changes to remote Dropbox since ``last_cursor``. Call this after
        :meth:`wait_for_remote_changes` returns ``True``.

        :param last_cursor: Last to cursor to compare for changes.
        :returns: Remote changes since given cursor.
        """

        results = [self.dbx.files_list_folder_continue(last_cursor)]

        while results[-1].has_more:
            more_results = self.dbx.files_list_folder_continue(results[-1].cursor)
            results.append(more_results)

        # combine all results into one
        results = self.flatten_results(results)

        return results


# ==== conversion functions to generate error messages and types =======================


def os_to_maestral_error(
    exc: OSError, dbx_path: Optional[str] = None, local_path: Optional[str] = None
) -> LocalError:
    """
    Converts a :class:`OSError` to a :class:`MaestralApiError` and tries to add a
    reasonably informative error title and message.

    .. note::
        The following exception types should not typically be raised during syncing:

        InterruptedError: Python will automatically retry on interrupted connections.
        NotADirectoryError: If raised, this likely is a Maestral bug.
        IsADirectoryError: If raised, this likely is a Maestral bug.

    :param exc: Python Exception.
    :param dbx_path: Dropbox path of file which triggered the error.
    :param local_path: Local path of file which triggered the error.
    :returns: :class:`MaestralApiError` instance or :class:`OSError` instance.
    """

    title = "Could not sync file or folder"
    err_cls: Type[MaestralApiError]

    if isinstance(exc, PermissionError):
        err_cls = InsufficientPermissionsError  # subclass of SyncError
        text = "Insufficient read or write permissions for this location."
    elif isinstance(exc, FileNotFoundError):
        err_cls = NotFoundError  # subclass of SyncError
        text = "The given path does not exist."
    elif isinstance(exc, FileExistsError):
        err_cls = ConflictError  # subclass of SyncError
        title = "Could not download file"
        text = "There already is an item at the given path."
    elif isinstance(exc, IsADirectoryError):
        err_cls = IsAFolderError  # subclass of SyncError
        title = "Could not create local file"
        text = "The given path refers to a folder."
    elif isinstance(exc, NotADirectoryError):
        err_cls = NotAFolderError  # subclass of SyncError
        title = "Could not create local folder"
        text = "The given path refers to a file."
    elif exc.errno == errno.ENAMETOOLONG:
        err_cls = PathError  # subclass of SyncError
        title = "Could not create local file"
        text = "The file name (including path) is too long."
    elif exc.errno == errno.EINVAL:
        err_cls = PathError  # subclass of SyncError
        title = "Could not create local file"
        text = (
            "The file name contains characters which are not allowed on your file "
            "system. This could be for instance a colon or a trailing period."
        )
    elif exc.errno == errno.EFBIG:
        err_cls = FileSizeError  # subclass of SyncError
        title = "Could not download file"
        text = "The file size too large."
    elif exc.errno == errno.ENOSPC:
        err_cls = InsufficientSpaceError  # subclass of SyncError
        title = "Could not download file"
        text = "There is not enough space left on the selected drive."
    elif exc.errno == errno.EFAULT:
        err_cls = FileReadError  # subclass of SyncError
        title = "Could not upload file"
        text = "An error occurred while reading the file content."
    elif exc.errno == errno.ENOMEM:
        err_cls = OutOfMemoryError  # subclass of MaestralApiError
        text = "Out of memory. Please reduce the number of memory consuming processes."
    else:
        return exc

    maestral_exc = err_cls(title, text, dbx_path=dbx_path, local_path=local_path)
    maestral_exc.__cause__ = exc

    return maestral_exc


def fswatch_to_maestral_error(exc: OSError) -> LocalError:
    """
    Converts a :class:`OSError` when starting a file system watch to a
    :class:`MaestralApiError` and tries to add a reasonably informative error title and
    message. Error messages and types differ from :func:`os_to_maestral_error`.

    :param exc: Python Exception.
    :returns: :class:`MaestralApiError` instance or :class:`OSError` instance.
    """

    error_number = getattr(exc, "errno", -1)
    err_cls: Type[MaestralApiError]

    if isinstance(exc, NotADirectoryError):
        title = "Dropbox folder has been moved or deleted"
        msg = (
            "Please move the Dropbox folder back to its original location "
            "or restart Maestral to set up a new folder."
        )

        err_cls = NoDropboxDirError
    elif isinstance(exc, PermissionError):
        title = "Insufficient permissions for Dropbox folder"
        msg = (
            "Please ensure that you have read and write permissions "
            "for the selected Dropbox folder."
        )
        err_cls = InsufficientPermissionsError

    elif error_number in (errno.ENOSPC, errno.EMFILE):
        title = "Inotify limit reached"
        if error_number == errno.ENOSPC:
            new_config = "fs.inotify.max_user_watches=524288"
        else:
            new_config = "fs.inotify.max_user_instances=512"
        msg = (
            "Changes to your Dropbox folder cannot be monitored because it "
            "contains too many items. Please increase the inotify limit in "
            "your system by adding the following line to /etc/sysctl.conf: "
            + new_config
        )
        err_cls = InotifyError

    else:
        return exc

    maestral_exc = err_cls(title, msg)
    maestral_exc.__cause__ = exc

    return maestral_exc


def dropbox_to_maestral_error(
    exc: exceptions.DropboxException,
    dbx_path: Optional[str] = None,
    local_path: Optional[str] = None,
) -> MaestralApiError:
    """
    Converts a Dropbox SDK exception to a :class:`MaestralApiError` and tries to add a
    reasonably informative error title and message.

    :param exc: :class:`dropbox.exceptions.DropboxException` instance.
    :param dbx_path: Dropbox path of file which triggered the error.
    :param local_path: Local path of file which triggered the error.
    :returns: :class:`MaestralApiError` instance.
    """

    err_cls: Type[MaestralApiError]
    # ---- Dropbox API Errors ----------------------------------------------------------
    if isinstance(exc, exceptions.ApiError):

        error = exc.error

        if isinstance(error, files.RelocationError):
            title = "Could not move file or folder"
            if error.is_cant_copy_shared_folder():
                text = "Shared folders can’t be copied."
                err_cls = SyncError
            elif error.is_cant_move_folder_into_itself():
                text = "You cannot move a folder into itself."
                err_cls = ConflictError
            elif error.is_cant_move_shared_folder():
                text = "You cannot move the shared folder to the given destination."
                err_cls = PathError
            elif error.is_cant_nest_shared_folder():
                text = (
                    "Your move operation would result in nested shared folders. "
                    "This is not allowed."
                )
                err_cls = PathError
            elif error.is_cant_transfer_ownership():
                text = (
                    "Your move operation would result in an ownership transfer. "
                    "Maestral does not currently support this. Please carry out "
                    "the move on the Dropbox website instead."
                )
                err_cls = PathError
            elif error.is_duplicated_or_nested_paths():
                text = (
                    "There are duplicated/nested paths among the target and "
                    "destination folders."
                )
                err_cls = PathError
            elif error.is_from_lookup():
                lookup_error = error.get_from_lookup()
                text, err_cls = _get_lookup_error_msg(lookup_error)
            elif error.is_from_write():
                write_error = error.get_from_write()
                text, err_cls = _get_write_error_msg(write_error)
            elif error.is_insufficient_quota():
                text = (
                    "You do not have enough space on Dropbox to move "
                    "or copy the files."
                )
                err_cls = InsufficientSpaceError
            elif error.is_internal_error():
                text = "Something went on Dropbox’s end. Please try again later."
                err_cls = DropboxServerError
            elif error.is_to():
                to_error = error.get_to()
                text, err_cls = _get_write_error_msg(to_error)
            elif error.is_too_many_files():
                text = (
                    "There are more than 10,000 files and folders in one "
                    "request. Please try to move fewer items at once."
                )
                err_cls = SyncError
            else:
                text = "Please check the logs for more information"
                err_cls = SyncError

        elif isinstance(error, (files.CreateFolderError, files.CreateFolderEntryError)):
            title = "Could not create folder"
            if error.is_path():
                write_error = error.get_path()
                text, err_cls = _get_write_error_msg(write_error)
            else:
                text = "Please check the logs for more information"
                err_cls = SyncError

        elif isinstance(error, files.DeleteError):
            title = "Could not delete item"
            if error.is_path_lookup():
                lookup_error = error.get_path_lookup()
                text, err_cls = _get_lookup_error_msg(lookup_error)
            elif error.is_path_write():
                write_error = error.get_path_write()
                text, err_cls = _get_write_error_msg(write_error)
            elif error.is_too_many_files():
                text = (
                    "There are more than 10,000 files and folders in one "
                    "request. Please try to delete fewer items at once."
                )
                err_cls = SyncError
            elif error.is_too_many_write_operations():
                text = (
                    "There are too many write operations happening in your "
                    "Dropbox. Please try again later."
                )
                err_cls = SyncError
            else:
                text = "Please check the logs for more information"
                err_cls = SyncError

        elif isinstance(error, files.UploadError):
            title = "Could not upload file"
            if error.is_path():
                write_error = error.get_path().reason  # returns UploadWriteFailed
                text, err_cls = _get_write_error_msg(write_error)
            elif error.is_properties_error():
                text = "Invalid property group provided."
                err_cls = SyncError
            else:
                text = "Please check the logs for more information"
                err_cls = SyncError

        elif isinstance(error, files.UploadSessionFinishError):
            title = "Could not upload file"
            if error.is_lookup_failed():
                session_lookup_error = error.get_lookup_failed()
                text, err_cls = _get_session_lookup_error_msg(session_lookup_error)
            elif error.is_path():
                write_error = error.get_path()
                text, err_cls = _get_write_error_msg(write_error)
            elif error.is_properties_error():
                text = "Invalid property group provided."
                err_cls = SyncError
            elif error.is_too_many_write_operations():
                text = (
                    "There are too many write operations happening in your "
                    "Dropbox. Please retry again later."
                )
                err_cls = SyncError
            else:
                text = "Please check the logs for more information"
                err_cls = SyncError

        elif isinstance(error, files.UploadSessionLookupError):
            title = "Could not upload file"
            text, err_cls = _get_session_lookup_error_msg(error)

        elif isinstance(error, files.DownloadError):
            title = "Could not download file"
            if error.is_path():
                lookup_error = error.get_path()
                text, err_cls = _get_lookup_error_msg(lookup_error)
            elif error.is_unsupported_file():
                text = "This file type cannot be downloaded but must be exported."
                err_cls = UnsupportedFileError
            else:
                text = "Please check the logs for more information"
                err_cls = SyncError

        elif isinstance(error, files.ListFolderError):
            title = "Could not list folder contents"
            if error.is_path():
                lookup_error = error.get_path()
                text, err_cls = _get_lookup_error_msg(lookup_error)
            else:
                text = (
                    "Please contact the developer with the traceback "
                    "information from the logs."
                )
                err_cls = MaestralApiError

        elif isinstance(error, files.ListFolderContinueError):
            title = "Could not list folder contents"
            if error.is_path():
                lookup_error = error.get_path()
                text, err_cls = _get_lookup_error_msg(lookup_error)
            elif error.is_reset():
                text = (
                    "Dropbox has reset its sync state. Please rebuild "
                    "Maestral's index to re-sync your Dropbox."
                )
                err_cls = CursorResetError
            else:
                text = (
                    "Please contact the developer with the traceback "
                    "information from the logs."
                )
                err_cls = MaestralApiError

        elif isinstance(error, files.ListFolderLongpollError):
            title = "Could not get Dropbox changes"
            if error.is_reset():
                text = (
                    "Dropbox has reset its sync state. Please rebuild "
                    "Maestral's index to re-sync your Dropbox."
                )
                err_cls = CursorResetError
            else:
                text = (
                    "Please contact the developer with the traceback "
                    "information from the logs."
                )
                err_cls = MaestralApiError

        elif isinstance(error, async_.PollError):

            title = "Could not get status of batch job"

            if error.is_internal_error():
                text = (
                    "Something went wrong with the job on Dropbox’s end. Please "
                    "verify on the Dropbox website if the job succeeded and try "
                    "again if it failed."
                )
                err_cls = DropboxServerError
            else:
                # Other tags include invalid_async_job_id. Neither should occur in our
                # SDK usage.
                text = (
                    "Please contact the developer with the traceback "
                    "information from the logs."
                )
                err_cls = MaestralApiError

        elif isinstance(error, files.ListRevisionsError):

            title = "Could not list file revisions"

            if error.is_path():
                lookup_error = error.get_path()
                text, err_cls = _get_lookup_error_msg(lookup_error)
            else:
                text = (
                    "Please contact the developer with the traceback "
                    "information from the logs."
                )
                err_cls = MaestralApiError

        elif isinstance(error, files.RestoreError):

            title = "Could not restore file"

            if error.is_invalid_revision():
                text = "Invalid revision."
                err_cls = PathError
            elif error.is_path_lookup():
                lookup_error = error.get_path_lookup()
                text, err_cls = _get_lookup_error_msg(lookup_error)
            elif error.is_path_write():
                write_error = error.get_path_write()
                text, err_cls = _get_write_error_msg(write_error)
            else:
                text = (
                    "Please contact the developer with the traceback "
                    "information from the logs."
                )
                err_cls = MaestralApiError

        elif isinstance(error, users.GetAccountError):
            title = "Could not get account info"

            if error.is_no_account():
                text = (
                    "An account with the given Dropbox ID does not "
                    "exist or has been deleted"
                )
                err_cls = InvalidDbidError
            else:
                text = (
                    "Please contact the developer with the traceback "
                    "information from the logs."
                )
                err_cls = MaestralApiError

        else:
            err_cls = MaestralApiError
            title = "An unexpected error occurred"
            text = (
                "Please contact the developer with the traceback "
                "information from the logs."
            )

    # ---- Authentication errors -------------------------------------------------------
    elif isinstance(exc, exceptions.AuthError):
        error = exc.error
        if isinstance(error, auth.AuthError):
            if error.is_expired_access_token():
                err_cls = TokenExpiredError
                title = "Authentication error"
                text = (
                    "Maestral's access to your Dropbox has expired. Please relink "
                    "to continue syncing."
                )
            elif error.is_invalid_access_token():
                err_cls = TokenRevokedError
                title = "Authentication error"
                text = (
                    "Maestral's access to your Dropbox has been revoked. Please "
                    "relink to continue syncing."
                )
            elif error.is_user_suspended():
                err_cls = DropboxAuthError
                title = "Authentication error"
                text = "Your user account has been suspended."
            else:
                # Other tags are invalid_select_admin, invalid_select_user,
                # missing_scope, route_access_denied. Neither should occur in our SDK
                # usage.
                err_cls = MaestralApiError
                title = "An unexpected error occurred"
                text = (
                    "Please contact the developer with the traceback "
                    "information from the logs."
                )

        else:
            err_cls = DropboxAuthError
            title = "Authentication error"
            text = (
                "Please check if you can log into your account on the Dropbox website."
            )

    # ---- OAuth2 flow errors ----------------------------------------------------------
    elif isinstance(exc, requests.HTTPError):
        err_cls = DropboxAuthError
        title = "Authentication failed"
        text = "Please make sure that you entered the correct authentication code."

    elif isinstance(exc, oauth.BadStateException):
        err_cls = DropboxAuthError
        title = "Authentication session expired."
        text = "The authentication session expired. Please try again."

    elif isinstance(exc, oauth.NotApprovedException):
        err_cls = DropboxAuthError
        title = "Not approved error"
        text = "Please grant Maestral access to your Dropbox to start syncing."

    # ---- Bad input errors ------------------------------------------------------------
    # should only occur due to user input from console scripts
    elif isinstance(exc, exceptions.BadInputError):
        err_cls = BadInputError
        title = "Bad input to API call"
        text = exc.message

    # ---- Internal Dropbox error ------------------------------------------------------
    elif isinstance(exc, exceptions.InternalServerError):
        err_cls = DropboxServerError
        title = "Could not sync file or folder"
        text = (
            "Something went wrong with the job on Dropbox’s end. Please "
            "verify on the Dropbox website if the job succeeded and try "
            "again if it failed."
        )

    # ---- Everything else -------------------------------------------------------------
    else:
        err_cls = MaestralApiError
        title = "An unexpected error occurred"
        text = (
            "Please contact the developer with the traceback "
            "information from the logs."
        )

    maestral_exc = err_cls(title, text, dbx_path=dbx_path, local_path=local_path)
    maestral_exc.__cause__ = exc

    return maestral_exc


def _get_write_error_msg(write_error: files.WriteError) -> Tuple[str, WriteErrorType]:

    text = ""
    err_cls = SyncError

    if write_error.is_conflict():
        conflict = write_error.get_conflict()
        if conflict.is_file():
            text = (
                "Could not write to the target path because another file "
                "was in the way."
            )
            err_cls = FileConflictError
        elif conflict.is_folder():
            text = (
                "Could not write to the target path because another folder "
                "was in the way."
            )
            err_cls = FolderConflictError
        elif conflict.is_file_ancestor():
            text = (
                "Could not create parent folders because another file "
                "was in the way."
            )
            err_cls = FileConflictError
        else:
            text = (
                "Could not write to the target path because another file or "
                "folder was in the way."
            )
            err_cls = ConflictError
    elif write_error.is_disallowed_name():
        text = "Dropbox will not save the file or folder because of its name."
        err_cls = PathError
    elif write_error.is_insufficient_space():
        text = "You do not have enough space on Dropbox to move or copy the files."
        err_cls = InsufficientSpaceError
    elif write_error.is_malformed_path():
        text = (
            "The destination path is invalid. Paths may not end with a slash or "
            "whitespace."
        )
        err_cls = PathError
    elif write_error.is_no_write_permission():
        text = "You do not have permissions to write to the target location."
        err_cls = InsufficientPermissionsError
    elif write_error.is_team_folder():
        text = "You cannot move or delete team folders through Maestral."
    elif write_error.is_too_many_write_operations():
        text = (
            "There are too many write operations in your Dropbox. Please "
            "try again later."
        )

    return text, err_cls


def _get_lookup_error_msg(
    lookup_error: files.LookupError,
) -> Tuple[str, LookupErrorType]:

    text = ""
    err_cls = SyncError

    if lookup_error.is_malformed_path():
        text = "The path is invalid. Paths may not end with a slash or whitespace."
        err_cls = PathError
    elif lookup_error.is_not_file():
        text = "The given path refers to a folder."
        err_cls = IsAFolderError
    elif lookup_error.is_not_folder():
        text = "The given path refers to a file."
        err_cls = NotAFolderError
    elif lookup_error.is_not_found():
        text = "There is nothing at the given path."
        err_cls = NotFoundError
    elif lookup_error.is_restricted_content():
        text = (
            "The file cannot be transferred because the content is restricted. For "
            "example, sometimes there are legal restrictions due to copyright "
            "claims."
        )
        err_cls = RestrictedContentError
    elif lookup_error.is_unsupported_content_type():
        text = "This file type is currently not supported for syncing."
        err_cls = UnsupportedFileError
    elif lookup_error.is_locked():
        text = "The given path is locked."
        err_cls = InsufficientPermissionsError

    return text, err_cls


def _get_session_lookup_error_msg(
    session_lookup_error: files.UploadSessionLookupError,
) -> Tuple[str, SessionLookupErrorType]:

    text = ""
    err_cls = SyncError

    if session_lookup_error.is_closed():
        # happens when trying to append data to a closed session
        # this is caused by internal Maestral errors
        pass
    elif session_lookup_error.is_incorrect_offset():
        text = "A network error occurred during the upload session."
    elif session_lookup_error.is_not_closed():
        # happens when trying to finish an open session
        # this is caused by internal Maestral errors
        pass
    elif session_lookup_error.is_not_found():
        text = (
            "The upload session ID was not found or has expired. "
            "Upload sessions are valid for 48 hours."
        )
    elif session_lookup_error.is_too_large():
        text = "You can only upload files up to 350 GB."
        err_cls = FileSizeError

    return text, err_cls

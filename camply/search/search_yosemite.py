"""
Yosemite National Park Lodging Search Utilities
"""

import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional, Set, Union

import pandas as pd

from camply.config.api_config import YosemiteConfig
from camply.containers import AvailableCampsite, RecreationArea, SearchWindow
from camply.exceptions import SearchError
from camply.providers.yosemite.yosemite_lodging import YosemiteLodging
from camply.search.base_search import BaseCampingSearch
from camply.utils import make_list
from camply.utils.logging_utils import log_sorted_response

logger = logging.getLogger(__name__)


class SearchYosemite(BaseCampingSearch):
    """
    Searches on reservations.ahlsmsworld.com for Yosemite Lodging
    """

    recreation_area = YosemiteLodging.recreation_area
    provider_class = YosemiteLodging
    list_campsites_supported: bool = False

    def __init__(
        self,
        search_window: Union[SearchWindow, List[SearchWindow]],
        weekends_only: bool = False,
        campgrounds: Optional[Union[List[str], str]] = None,
        nights: int = 1,
        offline_search: bool = False,
        offline_search_path: Optional[str] = None,
        **kwargs,
    ) -> None:
        """
        Initialize with Search Parameters

        Parameters
        ----------
        search_window: Union[SearchWindow, List[SearchWindow]]
            Search Window tuple containing start date and End Date
        weekends_only: bool
            Whether to only search for availabilities on weekends
        campgrounds: Optional[Union[List[str], str]]
            Property code or list of property codes (e.g., 'H' for Housekeeping Camp)
        nights: int
            Minimum number of consecutive nights, defaults to 1
        offline_search: bool
            Save/load results for offline use
        offline_search_path: Optional[str]
            File path for offline search results
        """
        super().__init__(
            search_window=search_window,
            weekends_only=weekends_only,
            nights=nights,
            offline_search=offline_search,
            offline_search_path=offline_search_path,
            **kwargs,
        )
        self.campgrounds = make_list(campgrounds)

    def get_all_campsites(self) -> List[AvailableCampsite]:
        """
        Search for all matching campsites in Yosemite.

        Returns
        -------
        List[AvailableCampsite]
        """
        all_campsites = []
        searchable_campgrounds = self._get_searchable_campgrounds()
        this_month = datetime.now().date().replace(day=1)
        for month in self.search_months:
            if month >= this_month:
                all_campsites += self.campsite_finder.get_monthly_campsites(
                    month=month, nights=None if self.nights == 1 else self.nights
                )
        matching_campsites = self._filter_campsites_to_campgrounds(
            campsites=all_campsites, searchable_campgrounds=searchable_campgrounds
        )
        campsite_df = self.campsites_to_df(campsites=matching_campsites)
        campsite_df_validated = self._filter_date_overlap(campsites=campsite_df)
        time_window_start = min(self.search_days)
        time_window_end = max(self.search_days) + timedelta(days=1)
        compiled_campsite_df = campsite_df_validated[
            (campsite_df_validated.booking_date >= pd.Timestamp(time_window_start))
            & (
                campsite_df_validated.booking_end_date
                <= pd.Timestamp(time_window_end)
            )
        ]
        compiled_campsites = self.df_to_campsites(campsite_df=compiled_campsite_df)
        return compiled_campsites

    def _get_searchable_campgrounds(self) -> Optional[Set[str]]:
        """
        Return the campground property codes for the search.

        Returns
        -------
        Optional[Set[str]]
        """
        if self.campgrounds in [None, []]:
            return None
        supported = set(YosemiteConfig.YOSEMITE_PROPERTIES.keys())
        selected = set(self.campgrounds)
        searchable = supported.intersection(selected)
        if len(searchable) == 0:
            property_ids = [
                f"`{key}` ({value})"
                for key, value in YosemiteConfig.YOSEMITE_PROPERTIES.items()
            ]
            error_message = (
                "You must supply a valid Yosemite property code. "
                "Supported property codes: "
                f"{', '.join(property_ids)}"
            )
            logger.error(error_message)
            raise SearchError(error_message)
        logger.info(f"{len(searchable)} Matching Properties Found")
        for prop_code in searchable:
            logger.info(
                f"⛰  {YosemiteConfig.YOSEMITE_RECREATION_AREA_FORMAL_NAME} "
                f"(#{YosemiteConfig.YOSEMITE_RECREATION_AREA_ID}) - 🏕  "
                f"{YosemiteConfig.YOSEMITE_PROPERTIES[prop_code]} ({prop_code})"
            )
        return searchable

    def _filter_campsites_to_campgrounds(
        self,
        campsites: List[AvailableCampsite],
        searchable_campgrounds: Optional[Set[str]],
    ) -> List[AvailableCampsite]:
        """
        Filter campsites down to matching property codes.

        Parameters
        ----------
        campsites: List[AvailableCampsite]
        searchable_campgrounds: Optional[Set[str]]

        Returns
        -------
        List[AvailableCampsite]
        """
        if self.campgrounds in [None, []]:
            return campsites
        return [
            campsite
            for campsite in campsites
            if campsite.facility_id in searchable_campgrounds
        ]

    @classmethod
    def find_recreation_areas(cls, **kwargs) -> List[RecreationArea]:
        """
        Return the Yosemite Recreation Area Object
        """
        log_sorted_response([cls.recreation_area])
        return [cls.recreation_area]

    def list_campsite_units(self) -> Any:
        """
        List Campsite Units

        Returns
        -------
        Any
        """
        raise NotImplementedError

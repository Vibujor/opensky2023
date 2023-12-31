from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
from traffic.core import Flight, FlightPlan, Traffic
from traffic.core.mixins import DataFrameMixin

from functions_heuristic import predict_fp

extent = "LFBBBDX"
prefix_sector = "LFBB"
margin_fl = 50  # margin for flight level
altitude_min = 20000
angle_precision = 2
forward_time = 20
min_distance = 200
nbworkers = 60


class Metadata(DataFrameMixin):
    def __getitem__(self, key: str) -> None | FlightPlan:
        df = self.data.query(f'flight_id == "{key}"')
        if df.shape[0] == 0:
            return None
        return FlightPlan(df.iloc[0]["route"])


def dist_lat_min(f1: Flight, f2: Flight) -> Any:
    try:
        if f1 & f2 is None:  # no overlap
            print(f"no overlap with {f2.flight_id}")
            return None
        return f1.distance(f2)["lateral"].min()
    except TypeError:
        print(
            f"exception in dist_lat_min for flights {f1.flight_id} and {f2.flight_id}"
        )
        return None


def extract_flight_deviations(
    flight: Flight,
    flightplan: FlightPlan,
    context_traffic: Traffic,
    margin_fl: int = 50,
    angle_precision: int = 2,
    min_distance: int = 200,
    forward_time: int = 20,
) -> None | pd.DataFrame:
    """
    Examines all deviations in flight and returns selected ones in a dataframe.

    :param flight: Flight of interest
    :param flightplan: Flight plan of flight
    :param context_traffic: Surrounding flights
    :param margin_fl: Margin in ft to check altitude stability
    :param angle_precision: Desired precision in alignment computation
    :param min_distance: Distance from which we consider a navpoint for alignment
    :param forward_time: Duration of trajectory prediction

    :return: None or DataFrame containing selected deviations
    """
    list_dicts = []
    for hole in flight - flight.aligned_on_navpoint(
        flightplan,
        angle_precision=angle_precision,
        min_distance=min_distance,
    ):
        temp_dict = hole.summary(["flight_id", "start", "stop", "duration"])
        temp_dict = {
            **temp_dict,
            **dict(
                min_f_dist=None,  # actual minimum separation
                min_fp_dist=None,  # predicted minimum separation
                min_fp_time=None,  # time at predicted minimum separation
                neighbour_id=None,  # flight_id of closest neighbour
            ),
        }
        if (
            hole is not None
            and hole.duration > pd.Timedelta("120s")
            and hole.altitude_max - hole.altitude_min < margin_fl
            and hole.start > flight.start
            and hole.stop < flight.stop
        ):
            flight = flight.resample("1s")
            hole = hole.resample("1s")

            flmin = hole.altitude_min - margin_fl
            flmax = hole.altitude_max + margin_fl

            horizon = min(
                hole.start + pd.Timedelta(minutes=forward_time),
                flight.stop,
            )
            flight_interest = flight.between(hole.start, horizon)
            assert flight_interest is not None

            # if the altitude changes significantly before horizon, we adjust horizon
            offlimits = flight_interest.query(f"altitude>{flmax} or altitude<{flmin}")
            if offlimits is not None:
                istop = offlimits.data.index[0]
                flight_interest.data = flight_interest.data.loc[:istop]
                horizon = flight_interest.stop

            # we select relevant flight portions in context
            neighbours = (
                (context_traffic - flight)
                .between(
                    start=hole.start,
                    stop=horizon,
                    strict=False,
                )
                .iterate_lazy()
                .query(f"{flmin} <= altitude <= {flmax}")
                .feature_gt("duration", datetime.timedelta(seconds=2))
                .eval()
            )

            pred_possible = flight.before(hole.start) is not None

            if neighbours is None and not pred_possible:
                continue

            if pred_possible:
                pred_fp = predict_fp(
                    flight,
                    flightplan,
                    hole.start,
                    hole.stop,
                    minutes=forward_time,
                    min_distance=min_distance,
                )

            if neighbours is not None:
                (min_f, idmin_f) = min(
                    (dist_lat_min(flight_interest, f), f.flight_id) for f in neighbours
                )
                temp_dict["neighbour_id"] = idmin_f
                temp_dict["min_f_dist"] = min_f
                df_dist = flight_interest.distance(neighbours[idmin_f])
                temp_dict["min_f_time"] = df_dist.loc[
                    df_dist.lateral == df_dist.lateral.min()
                ].timestamp.iloc[0]

                if pred_possible:
                    df_dist_fp = pred_fp.distance(neighbours[idmin_f])
                    temp_dict["min_fp_dist"] = df_dist_fp.lateral.min()
                    temp_dict["min_fp_time"] = df_dist_fp.loc[
                        df_dist_fp.lateral == df_dist_fp.lateral.min()
                    ].timestamp.iloc[0]

            list_dicts.append(temp_dict)
    if len(list_dicts) == 0:
        return None
    deviations = pd.DataFrame(list_dicts)
    # we compute the difference between actual and predicted separation
    deviations["difference"] = deviations["min_f_dist"] - deviations["min_fp_dist"]
    # we clear the cases for which trajectories exist more than once
    deviations = deviations[deviations.min_f_dist != 0.0]
    return deviations


def extract_traffic_deviations(
    flights: Traffic,
    metadata_file: Path,
    context_traffic: Traffic,
    margin_fl: int = 50,
    angle_precision: int = 2,
    min_distance: int = 200,
    forward_time: int = 20,
) -> None | pd.DataFrame:
    """
    Examines all deviations in flight and returns selected ones in a dataframe.

    :param flights: All flights to examine
    :param metadata_file: File linking flight_id and flight plan (route)
    :param context_traffic: Surrounding flights to take into consideration
    :param margin_fl: Margin in ft to check altitude stability
    :param angle_precision: Desired precision in alignment computation
    :param min_distance: Distance from which we consider a navpoint for alignment
    :param forward_time: Duration of trajectory prediction

    :return: None or DataFrame containing selected deviations
    """
    cumul_deviations = []
    metadata = pd.read_parquet(metadata_file)

    metadata_simple = Metadata(
        metadata.groupby("flight_id", as_index=False)
        .last()
        .eval("icao24 = icao24.str.lower()")
    )

    for flight in flights:
        try:
            df = extract_flight_deviations(
                flight,
                metadata_simple[flight.flight_id],
                context_traffic,
            )
            if df is not None:
                cumul_deviations.append(df)
        except AssertionError:
            print(f"AssertionError in main for flight{flight.flight_id}")
        except TypeError as e:
            print(f"TypeError in main for flight {flight.flight_id}")
        except AttributeError as e:
            print(f"AttributeError in main for flight {flight.flight_id}")
    if not cumul_deviations:
        return None
    all_deviations = pd.concat(cumul_deviations, ignore_index=True)
    return all_deviations

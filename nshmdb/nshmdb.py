"""
Module to interact with the NSHMDB (National Seismic Hazard Model Database).

This module provides classes and functions to interact with an SQLite database
containing national seismic hazard model data. It includes functionalities to
insert fault and rupture data into the database, as well as retrieve fault
information associated with ruptures.

Classes
-------
NSHMDB
    Class for interacting with the NSHMDB database.

Usage
-----
Initialize an instance of NSHMDB with the path to the SQLite database file.
Use the methods of the NSHMDB class to interact with fault and rupture data
in the database.

>>> db = NSHMDB('path/to/nshm.db')
>>> db.get_rupture_faults(0) # Should return two faults in this rupture.
"""

import dataclasses
import importlib.resources
import sqlite3
from pathlib import Path
from sqlite3 import Connection

import numpy as np
import qcore.coordinates

from nshmdb import fault
from nshmdb.fault import Fault


@dataclasses.dataclass
class NSHMDB:
    """Class for interacting with the NSHMDB database.

    Parameters
    ----------
        db_filepath : Path
            Path to the SQLite database file.
    """

    db_filepath: Path

    def create(self):
        """Create the tables for the NSHMDB database."""
        schema_traversable = importlib.resources.files("nshmdb.schema") / "schema.sql"
        with importlib.resources.as_file(schema_traversable) as schema_path:
            with open(schema_path, "r", encoding="utf-8") as schema_file_handle:
                schema = schema_file_handle.read()
        with self.connection() as conn:
            conn.executescript(schema)

    def connection(self) -> Connection:
        """Establish a connection to the SQLite database.

        Returns
        -------
        Connection
        """
        return sqlite3.connect(self.db_filepath)

    # The functions `insert_parent`, `insert_fault`, and `add_fault_to_rupture`
    # reuse a connection for efficiency (rather than use db.connection()). There
    # are thousands of faults and tens of millions of rupture, fault binding
    # pairs. Without reusing a connection it takes hours to setup the database.

    def insert_parent(self, conn: Connection, parent_id: int, parent_name: str):
        """Insert parent fault data into the database.

        Parameters
        ----------
        conn : Connection
            The db connection object.
        parent_id : int
            ID of the parent fault.
        name : str
            Name of the parent fault.
        """
        conn.execute(
            """INSERT OR REPLACE INTO parent_fault (parent_id, name) VALUES (?, ?)""",
            (parent_id, parent_name),
        )

    def insert_fault(
        self, conn: Connection, fault_id: int, parent_id: int, fault: Fault
    ):
        """Insert fault data into the database.

        Parameters
        ----------
        conn : Connection
            The db connection object.
        fault_id : int
            ID of the fault.
        parent_id : int
            ID of the parent fault.
        fault : Fault
            Fault object containing fault geometry.
        """
        conn.execute(
            """INSERT OR REPLACE INTO fault (fault_id, name, parent_id) VALUES (?, ?, ?)""",
            (fault_id, fault.name, parent_id),
        )
        for plane in fault.planes:
            conn.execute(
                """INSERT INTO fault_plane (
                    top_left_lat,
                    top_left_lon,
                    top_right_lat,
                    top_right_lon,
                    bottom_right_lat,
                    bottom_right_lon,
                    bottom_left_lat,
                    bottom_left_lon,
                    top_depth,
                    bottom_depth,
                    rake,
                    fault_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    *plane.corners[:, :2].ravel(),
                    plane.corners[0, 2],
                    plane.corners[-1, 2],
                    plane.rake,
                    fault_id,
                ),
            )

    def add_fault_to_rupture(self, conn: Connection, rupture_id: int, fault_id: int):
        """Insert rupture data into the database.

        Parameters
        ----------
        conn : Connection
            The db connection object.
        rupture_id : int
            ID of the rupture.
        fault_ids : list[int]
            List of faults involved in the rupture.
        """
        conn.execute(
            "INSERT OR REPLACE INTO rupture (rupture_id) VALUES (?)", (rupture_id,)
        )
        conn.execute(
            "INSERT INTO rupture_faults (rupture_id, fault_id) VALUES (?, ?)",
            (rupture_id, fault_id),
        )

    def get_fault(self, fault_id: int) -> Fault:
        """Get a specific fault definition from a database.

        Parameters
        ----------
        fault_id : int
            The id of the fault to retreive.

        Returns
        -------
        Fault
            The fault geometry.
        """

        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * from fault_plane where fault_id = ?", (fault_id,))
            planes = []
            for (
                _,
                top_left_lat,
                top_left_lon,
                top_right_lat,
                top_right_lon,
                bottom_right_lat,
                bottom_right_lon,
                bottom_left_lat,
                bottom_left_lon,
                top,
                bottom,
                rake,
                _,
            ) in cursor.fetchall():
                corners = np.array(
                    [
                        [top_left_lat, top_left_lon, top],
                        [top_right_lat, top_right_lon, top],
                        [bottom_right_lat, bottom_right_lon, bottom],
                        [bottom_left_lat, bottom_left_lon, bottom],
                    ]
                )
                planes.append(
                    fault.FaultPlane(qcore.coordinates.wgs_depth_to_nztm(corners), rake)
                )
            cursor.execute("SELECT * from fault where fault_id = ?", (fault_id,))
            fault_id, name, _, _ = cursor.fetchone()
            return Fault(name, None, planes)

    def get_rupture_faults(self, rupture_id: int) -> list[Fault]:
        """Retrieve faults involved in a rupture from the database.

        Parameters
        ----------
        rupture_id : int

        Returns
        -------
        list[Fault]
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT fs.*, p.parent_id, p.name
                FROM fault_plane fs
                JOIN rupture_faults rf ON fs.fault_id = rf.fault_id
                JOIN fault f ON fs.fault_id = f.fault_id
                JOIN parent_fault p ON f.parent_id = p.parent_id
                WHERE rf.rupture_id = ?
                ORDER BY f.parent_id""",
                (rupture_id,),
            )
            fault_planes = cursor.fetchall()
            cur_parent_id = None
            faults = []
            for (
                _,
                top_left_lat,
                top_left_lon,
                top_right_lat,
                top_right_lon,
                bottom_right_lat,
                bottom_right_lon,
                bottom_left_lat,
                bottom_left_lon,
                top,
                bottom,
                rake,
                _,
                parent_id,
                parent_name,
            ) in fault_planes:
                if parent_id != cur_parent_id:
                    faults.append(
                        Fault(
                            name=parent_name,
                            tect_type=None,
                            planes=[],
                        )
                    )
                    cur_parent_id = parent_id
                corners = np.array(
                    [
                        [top_left_lat, top_left_lon, top],
                        [top_right_lat, top_right_lon, top],
                        [bottom_right_lat, bottom_right_lon, bottom],
                        [bottom_left_lat, bottom_left_lon, bottom],
                    ]
                )
                faults[-1].planes.append(
                    fault.FaultPlane(qcore.coordinates.wgs_depth_to_nztm(corners), rake)
                )
            return faults

import { Button, IconButton, Table, TableBody, TableCell, TableHead, TableRow } from '@mui/material';
import KeyboardArrowUpIcon from '@mui/icons-material/KeyboardArrowUp';
import KeyboardArrowDownIcon from '@mui/icons-material/KeyboardArrowDown';
import React, { useState, useEffect } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';

function AdminEnrollmentManagement() {
    const [data, setData] = useState([]);
    const [searchParams, setSearchParams] = useSearchParams();

    const email = searchParams.get('email');

    const handleApprove = async (row) => {
      console.log('Approving enrollment request for ' + row.email);
      console.log(row)

      await fetch(`https://vgyc6fujod.execute-api.us-east-1.amazonaws.com/prod/enroll/${row.requestid}`, {
        method: "PUT",
        body: JSON.stringify({
            response: "Approved"
        }),
      })
        .then((response) => response.json())
        .then((data) => console.log(data))
        .catch((error) => console.error(error));

      fetchData()
        .catch((error) => console.error(error));
    }

    const handleDeny = async (row) => {
      console.log('Denying enrollment request for ' + row.email);
      console.log(row.requestid)
      await fetch(`https://vgyc6fujod.execute-api.us-east-1.amazonaws.com/prod/enroll/${row.requestid}`, {
        method: "PUT",
        body: JSON.stringify({
            response: "Denied"
        }),
      })
        .then((response) => response.json())
        .then((data) => console.log(data))
        .catch((error) => console.error(error));

      fetchData()
        .catch((error) => console.error(error));
    }

    const ExpandableTableRow = ({ rowEmail, children, expandComponent, ...otherProps }) => {
        const [isExpanded, setIsExpanded] = React.useState(false);
      
        return (
          <>
            <TableRow {...otherProps}>
              <TableCell padding="checkbox">
                <IconButton onClick={() => setIsExpanded(!isExpanded)}>
                  {isExpanded || email === rowEmail ? <KeyboardArrowUpIcon /> : <KeyboardArrowDownIcon />}
                </IconButton>
              </TableCell>
              {children}
            </TableRow>
            {isExpanded && (
              <TableRow>
                <TableCell padding="checkbox" />
                {expandComponent}
              </TableRow>
            )}
          </>
        );
    };

    const fetchData = async () => {
        await fetch("https://vgyc6fujod.execute-api.us-east-1.amazonaws.com/prod/enroll")
          .then((response) => response.json())
          .then((data) => setData(data))
    }

    useEffect(() => {
        fetchData()
            .catch((error) => console.error(error));
    }, []);

    return (
        <div>
            <center>
                <h1>Admin Enrollment Management</h1>
                <Table sx={{ minWidth: 650 }} aria-label="simple table">
                    <TableHead>
                    <TableRow>
                        <TableCell padding="checkbox" />
                        <TableCell align="right">Name</TableCell>
                        <TableCell align="right">Email</TableCell>
                        <TableCell align="right">Role</TableCell>
                        <TableCell align="right">Status</TableCell>
                        <TableCell align="right">Date Created</TableCell>
                        <TableCell align="right">Approve</TableCell>
                        <TableCell align="right">Deny</TableCell>
                    </TableRow>
                    </TableHead>
                    <TableBody>
                    {data.filter((request) => (request.enrollmentstatus == 'Pending')).map((row) => (
                        <ExpandableTableRow
                        email={row.email}
                        key={row.requestid}
                        expandComponent={<TableCell colSpan="5">{row.message}</TableCell>}
                        >
                            <TableCell align="right">{row.lastName}, {row.firstName}</TableCell>
                            <TableCell align="right">{row.email}</TableCell>
                            <TableCell align="right">{row.role}</TableCell>
                            <TableCell align="right">{row.enrollmentstatus}</TableCell>
                            <TableCell align="right">{row.dateCreated}</TableCell>
                            <TableCell align="right"><Button variant="contained" color="primary" onClick={() => handleApprove(row)}>Approve</Button></TableCell>
                            <TableCell align="right"><Button variant="contained" color="secondary" onClick={() => handleDeny(row)}>Deny</Button></TableCell>
                        </ExpandableTableRow>
                    ))}
                    </TableBody>
                </Table>
            </center>
        </div>
    );
}

export default AdminEnrollmentManagement;

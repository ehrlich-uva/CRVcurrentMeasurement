<b>This project is a GUI for measuring the CRV SiPM currents. </b>

Screenshot of the GUI  
<img width="500" alt="currentMeasurement" src="https://github.com/user-attachments/assets/5129fd98-461d-45b2-bbf7-4b1f0cb1c80e" />  
  
  
The project requires two files: currentMeasurement.html and currentMeasurement.py  
They need to be located in the same directoy at mu2e-dcs-01.fnal.gov 

To start the GUI do the following steps.

1. Create a tunnel to port 3300 at mu2e-dcs-01.fnal.gov.  
   This port number is just temporary. We may want to find a more permanent one.  
    
   Option 1  
   In .ssh/config
   <pre>Host mu2e-dcs-01  
     HostName mu2e-dcs-01.fnal.gov  
     User mu2ecrv  
     LocalForward 3300 mu2e-dcs-01.fnal.gov:3300  
     ProxyCommand ssh -X mu2ecrv@mu2egateway01.fnal.gov -W %h:%p</pre>  
   Then `ssh mu2e-dcs-01`
   
   Option 2  
   `ssh -L 3300:mu2e-dcs-01.fnal.gov:3300 -J mu2ecrv@mu2egateway01.fnal.gov mu2ecrv@mu2e-dcs-01.fnal.gov`

2. Start the program  
   <pre>cd path/to/files  
   python3 currentMeasurement.py</pre>

3. Start the GUI  
   Open a browser tab and go to `localhost:3300`

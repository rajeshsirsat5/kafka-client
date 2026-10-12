I don't really think we need the retry at the consumer level. The reason I think it's something like 
this: let's consider a request flow. I received a message, parsed it, and I'm not going to parse it. 
As there is an error it will directly go to DLQ.
The other message that I got is a good message. I can parse it and I can map the data. I reached the 
create report stage. This create report itself will have 3 retries in the while loop:
1. It didn't connect.
2. It connected, created the report, and gave me the report ID.
Now I will reach the second stage to create discrepancies. Again it will have 3 retries. For each 
discrepancy I will keep on hitting the endpoint. Say, for example, all the discrepancies are created. 
Then I will go to the third stage, click Complete Report, and it is again the same happy flow. It 
connected in one shot and everything is done. I have created the report, all the things are done, and 
the message is consumed and acknowledged.

On the second flow, If the create report endpoint itself is not getting connected, if we keep 
retrying indefinitely in the while loop, what will happen? Ultimately as you said, if you are not 
reaching back to the broker, it may end and it will rebalance but we have a way to avoid it, right? 
We can simply keep polling the broker in between so that we will retain the connection and we can 
keep trying the Kafka company house URL.

In this while loop if there are 3 retries that don't give me the output or aren't reachable, then I 
will create an alert so that the SRE team can look into this issue. Meanwhile we'll keep on trying. 
The same happens for create discrepancies and the complete report as well.

In this case we are not losing the data at all and we are not missing the acknowledgement part either 
because everything will be done in a single sequence. This is the probable solution I see now. This 
is just out-of-the-box thinking for this while loop and independent retry. I just wanted to have a 
look and think critically on this part. Is it really worth doing this way? In this case we do not 
have any retry at the Kafka level or consumer level but we will have it internally. In the current 
solution, that's what it suits. That is what I feel because we are not going to acknowledge the 
messages if there is really any issue with the company's house API. 
